"""搜索分级预算及本程序子进程树的超时清理。"""
from __future__ import annotations

import math
import ctypes
import multiprocessing
import os
import signal
from statistics import median
import subprocess
import time

_SUPERVISED = False


class SearchBudget:
    """600 秒停止开新工作；近期改进可延至 900，已启动工作至多 1200。"""

    def __init__(self, target, started=None, *, extension=None, hard=None):
        extension = target * 1.5 if extension is None else extension
        hard = target * 2 if hard is None else hard
        if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0
               for v in (target, extension, hard)) or not target <= extension <= hard:
            raise ValueError("budget requires finite 0 < target <= extension <= hard")
        self.target = target
        self.extension = extension
        self.hard = hard
        self.started = time.perf_counter() if started is None else started
        self.last_gain = None
        self.last_gain_source = None
        self.costs = []
        self.timeout_floor = 0
        self.extended = False
        self.stop_reason = "search_exhausted"

    def elapsed(self):
        return time.perf_counter() - self.started

    def can_start(self, estimate=0):
        elapsed = self.elapsed()
        if elapsed >= self.hard:
            self.stop_reason = "hard_limit"
            return False
        if elapsed >= self.extension:
            self.stop_reason = "extension_limit"
            return False
        if elapsed + estimate >= self.target:
            if self.last_gain is not None and elapsed - self.last_gain <= self.target * .25:
                self.extended = True
            if not self.extended:
                self.stop_reason = "target_limit" if elapsed >= self.target else "insufficient_candidate_time"
                return False
        limit = self.extension if self.extended else self.target
        if elapsed + estimate >= limit:
            self.stop_reason = "insufficient_candidate_time"
            return False
        return True

    def candidate_cap(self):
        remaining = max(0, self.hard - self.elapsed())
        if not self.costs and not self.timeout_floor:
            return remaining
        # 用近期实际成本取代固定 30%；失败超时后放宽，避免重复同一过短限制。
        return min(remaining, max(5., self.timeout_floor,
                                 2 * median(self.costs[-5:]) if self.costs else 0,
                                 1.25 * self.costs[-1] if self.costs else 0))

    def observe(self, previous, result, record):
        if result is not None:
            if record.get("seconds", 0) > 0 and record.get('evaluation_source')!='approximate':
                self.costs.append(record["seconds"])
            if (previous and previous["makespan"] > 0 and
                    (previous["makespan"] - result["makespan"]) / previous["makespan"] >= .005):
                self.last_gain = self.elapsed()
                self.last_gain_source = record.get('evaluation_source','official')
        elif record.get("timed_out"):
            self.timeout_floor = max(self.timeout_floor, 2 * record["timeout_seconds"])

    def metadata(self):
        return {"time_policy": "tiered_v1", "target_seconds": self.target,
                "extension_seconds": self.extension, "hard_limit_seconds": self.hard,
                "extended": self.extended, "stop_reason": self.stop_reason,
                "last_gain_source": self.last_gain_source}


class CaseSliceBudget(SearchBudget):
    """A serial core search shares the case clock and leaves time for later cores.

    All public limits are relative to this slice's start, as in SearchBudget.
    Unused time returns to the case; gains may extend the case, never create four
    independent 600/900/1200-second budgets.
    """
    shared_case = True

    def __init__(self, parent, remaining_cores):
        if remaining_cores < 1:
            raise ValueError('remaining_cores must be positive')
        self.parent = parent
        now = time.perf_counter()
        case_elapsed = parent.elapsed()
        limit = parent.extension if parent.extended else parent.target
        share = max(.001, (limit - case_elapsed) / remaining_cores)
        super().__init__(min(max(600., parent.target), share), started=now)
        self.extension = min(self.target * (parent.extension / parent.target),
                             max(0., (parent.extension-case_elapsed)/remaining_cores))
        self.hard = min(self.target * (parent.hard / parent.target), max(0., parent.hard-case_elapsed))
        self.allocated_seconds = self.target

    def can_start(self, estimate=0):
        if not self.parent.can_start(estimate):
            self.stop_reason = 'case_' + self.parent.stop_reason
            return False
        return super().can_start(estimate)

    def observe(self, previous, result, record):
        super().observe(previous, result, record)
        self.parent.observe(previous, result, record)

    def metadata(self):
        return dict(super().metadata(), budget_scope='case_slice',
                    case_elapsed_seconds=self.parent.elapsed(),
                    case_target_seconds=self.parent.target,
                    case_extended=self.parent.extended,
                    allocated_seconds=self.allocated_seconds)


class _WindowsJob:
    """Windows 8+ 嵌套 Job：关闭句柄回收所有后代，无需枚举系统进程。"""

    def __init__(self):
        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", ctypes.c_ulong), ("min_working_set", ctypes.c_size_t),
                        ("max_working_set", ctypes.c_size_t), ("active_processes", ctypes.c_ulong),
                        ("affinity", ctypes.c_size_t), ("priority", ctypes.c_ulong),
                        ("scheduling", ctypes.c_ulong)]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", ctypes.c_uint64 * 6),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        self.api.CreateJobObjectW.restype = ctypes.c_void_p
        self.api.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
        self.api.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.api.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        self.api.OpenProcess.restype = ctypes.c_void_p
        self.api.CloseHandle.argtypes = [ctypes.c_void_p]
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def attach(self, pid):
        process = self.api.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self.api.AssignProcessToJobObject(self.handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.api.CloseHandle(process)

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def terminate_tree(process, job=None, *, group=True):
    """仅回收本程序创建的 Job / 进程组。"""
    try:
        if os.name == "nt":
            if job is not None:
                job.close()
        elif group:
            os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    finally:
        try:
            process.kill()
        except ProcessLookupError:
            pass


def _process_entry(target, args, ready):
    global _SUPERVISED
    if os.name != "nt":
        os.setsid()
    _SUPERVISED = True
    ready.wait()
    target(*args)


def run_bounded(target, args, deadline, telemetry=None):
    """外层监督覆盖同步候选生成；无需搜索代码主动检查就能中断。"""
    if time.perf_counter() >= deadline:
        return True
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    job = _WindowsJob() if os.name == "nt" else None
    process = context.Process(target=_process_entry, args=(target, args, ready))
    try:
        process.start()
        if job is not None:
            job.attach(process.pid)
        ready.set()
        process.join(max(0, deadline - time.perf_counter()))
        expired = process.is_alive()
        if expired:
            terminate_tree(process, job)
            process.join(10)
        elif process.exitcode:
            raise RuntimeError(f"search worker exited with code {process.exitcode}")
        if telemetry is not None and os.name == 'nt':
            # Query the retained process HANDLE even after exit/termination.
            api = ctypes.WinDLL('kernel32', use_last_error=True)
            api.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_uint64)] * 4
            created, ended, kernel, user = (ctypes.c_uint64() for _ in range(4))
            if api.GetProcessTimes(process._popen._handle, ctypes.byref(created), ctypes.byref(ended),
                                   ctypes.byref(kernel), ctypes.byref(user)):
                telemetry['cpu_seconds'] = (kernel.value + user.value) / 10_000_000
        return expired
    finally:
        if process.pid is not None and process.is_alive():
            terminate_tree(process, job)
            process.join(10)
        process.close()
        if job is not None:
            job.close()


def run_command(command, *, timeout, **kwargs):
    """subprocess.run 的受控版本：超时清理整个进程树（含 Windows venv 启动器）。"""
    job = _WindowsJob() if os.name == "nt" else None
    own_group = not _SUPERVISED
    try:
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              start_new_session=os.name != "nt" and own_group, **kwargs) as process:
            try:
                if job is not None:
                    job.attach(process.pid)
                stdout, stderr = process.communicate(timeout=timeout)
            except BaseException:
                terminate_tree(process, job, group=own_group)
                process.communicate()
                raise
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        if job is not None:
            job.close()
