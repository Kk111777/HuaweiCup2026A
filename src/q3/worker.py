"""Persistent case-local preparation with a killable parent-side deadline."""
import contextlib
import io
import multiprocessing
from pathlib import Path
import time
import traceback

from .artifacts import write_json
from q2.official import read_json, Q2Error
from q2.state import plan_digest
from .official import Settings, Evaluator, evaluate_fresh, objective
from .proposals import candidates, diagnose


def serve(connection, graph_path, reference, config, options, directory):
    try:
        settings = Settings.read(reference, config)
        evaluator = Evaluator(read_json(graph_path), settings, options['cache_bytes'])
        from evaluation_validation import EvaluationValidationError
        from stub_multicore_cut_and_schedule import MulticoreCutError
        from schedule_step2 import Step2SchedulingError
        from schedule_step3 import Step3SchedulingError
        from multicore_cut_evaluate_problem_2 import SceneBEvaluationError as Scene2Error
        from multicore_cut_evaluate_problem_3 import SceneBEvaluationError as Scene3Error
        invalid = (EvaluationValidationError, MulticoreCutError, Step2SchedulingError,
                   Step3SchedulingError, Scene2Error, Scene3Error, Q2Error)
        generators, serial = {}, 0
        connection.send(dict(status='READY'))
        while True:
            request = connection.recv()
            if request['action'] == 'stop':
                break
            start = time.monotonic()
            try:
                plan = read_json(request['plan'])
                action = request['action']
                with contextlib.redirect_stderr(io.StringIO()):
                    if action == 'evaluate':
                        if request.get('problem') == 2:
                            result = evaluate_fresh(evaluator.raw, plan, settings, problem=2)
                            prepared, status = None, 'OFFICIAL'
                        else:
                            result, prepared, status = evaluator.evaluate(plan, request.get('incumbent'), options['use_bounds'])
                        reply = dict(status=status, lower_bound=prepared.lower_bound if prepared else None,
                                     lower_parts=prepared.lower_parts if prepared else None)
                        if result is not None:
                            reply.update(objective=objective(result), cache_stats=result.get('cache_stats'))
                            # Full timelines are materialized only for an accepted
                            # improvement (or an explicitly requested evaluation).
                            if request.get('incumbent') is None or objective(result) < tuple(request['incumbent']):
                                path = Path(request['output'])
                                write_json(path, result)
                                reply.update(result=str(path), diagnostics=diagnose(result, prepared) if prepared else None)
                    elif action == 'next':
                        key = (plan_digest(plan), request['stage'])
                        if key not in generators:
                            prepared = evaluator.prepare(plan)
                            result = read_json(request['result'])
                            generators[key] = iter(candidates(evaluator, plan, result, prepared, request['stage'], options))
                            # At most three domains for the five active core counts.
                            if len(generators) > 15:
                                generators.pop(next(iter(generators)))
                        proposals, exhausted = [], False
                        for _ in range(request['count']):
                            try:
                                family, new = next(generators[key])
                            except StopIteration:
                                exhausted = True
                                break
                            serial += 1
                            path = Path(directory) / f'proposal_{serial:06d}.json'
                            write_json(path, new)
                            proposals.append(dict(family=family, path=str(path), digest=plan_digest(new)))
                        reply = dict(status='PROPOSALS', proposals=proposals, exhausted=exhausted)
                    elif action == 'audit':
                        from .validation import audit_result
                        prepared = evaluator.prepare(plan)
                        result = read_json(request['result'])
                        reply = dict(status='AUDITED', audit=audit_result(prepared.bundle, result, settings))
                    else:
                        raise ValueError('unknown worker command')
                reply.update(seconds=time.monotonic() - start, cache=evaluator.service.cache.record())
            except invalid as error:
                reply = dict(status='UNKNOWN' if 'max_iter exceeded' in str(error) else 'INVALID',
                             error=str(error), seconds=time.monotonic() - start)
            except (MemoryError, TimeoutError) as error:
                reply = dict(status='UNKNOWN', error=type(error).__name__, seconds=time.monotonic() - start)
            except BaseException:
                reply = dict(status='INTERNAL_ERROR', error=traceback.format_exc())
            connection.send(reply)
    except (EOFError, BrokenPipeError):
        pass
    except BaseException:
        try:
            connection.send(dict(status='INTERNAL_ERROR', error=traceback.format_exc()))
        except (EOFError, BrokenPipeError):
            pass
    finally:
        connection.close()


class Worker:
    def __init__(self, graph, settings, options, directory):
        self.args = (str(graph), str(settings.scene.root), str(settings.scene.config), options, str(directory))
        self.process = self.connection = None

    def close(self):
        if self.process is not None:
            if self.process.is_alive():
                self.process.terminate()
            self.process.join(timeout=5)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=5)
            self.process.close()
            self.process = None
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def _receive(self, deadline):
        while time.monotonic() < deadline:
            if self.connection.poll(min(.05, max(0, deadline - time.monotonic()))):
                result = self.connection.recv()
                if result['status'] == 'INTERNAL_ERROR':
                    raise RuntimeError(result['error'])
                return result
            if not self.process.is_alive():
                raise RuntimeError('Q3 evaluation worker exited without a result')
        raise TimeoutError('Q3 preparation/evaluation timed out')

    def call(self, request, timeout):
        if timeout <= 0:
            raise TimeoutError('Q3 case budget exhausted')
        deadline = time.monotonic() + timeout
        try:
            if self.process is None:
                context = multiprocessing.get_context('spawn')
                self.connection, child = context.Pipe()
                self.process = context.Process(target=serve, args=(child, *self.args))
                self.process.start()
                child.close()
                self._receive(deadline)
            self.connection.send(request)
            return self._receive(deadline)
        except BaseException:
            self.close()
            raise
