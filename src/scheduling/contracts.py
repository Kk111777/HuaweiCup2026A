"""Explicit, immutable bindings between the search coordinator and a problem.

The common coordinator handles budget, incumbent selection and audit. A backend
owns plan meaning, execution, prediction and candidate generation. Callables
keep their existing signatures so Q1 does not need algorithmic rewrites.

Result dictionaries expose ``makespan`` and ``evaluation_source`` to the search
policy. Estimators expose estimate/observe/result, stats and a plans cache whose
optional diagnostic entries contain raw_cycles and pressure_risk. These are
search-facing diagnostics, not permission to interpret another scene's traces.
"""
from dataclasses import dataclass
from typing import Any, Callable


def _callables(instance, names):
    for name in names:
        if not callable(getattr(instance, name)):
            raise TypeError(f'{type(instance).__name__}.{name} must be callable')


@dataclass(frozen=True)
class PlanSemantics:
    validate: Callable[..., Any]
    key: Callable[..., Any]
    objective: Callable[..., Any]
    state_signature: Callable[..., Any]

    def __post_init__(self):
        _callables(self, ('validate', 'key', 'objective', 'state_signature'))


@dataclass(frozen=True)
class ExecutionModel:
    read_settings: Callable[..., Any]
    settings_dict: Callable[..., dict]
    create_model: Callable[..., Any]
    create_index: Callable[..., Any]
    evaluate: Callable[..., dict]
    preanalyze: Callable[..., Any]
    preanalysis_key: Callable[..., Any]
    errors: tuple[type[Exception], ...]
    empty_result: Callable[[int], dict]

    def __post_init__(self):
        _callables(self, ('read_settings', 'settings_dict', 'create_model', 'create_index',
                         'evaluate', 'preanalyze', 'preanalysis_key', 'empty_result'))
        if (not isinstance(self.errors, tuple) or
                any(not isinstance(error, type) or not issubclass(error, Exception)
                    for error in self.errors)):
            raise TypeError('ExecutionModel.errors must be a tuple of Exception types')


@dataclass(frozen=True)
class PredictionModel:
    create_estimator: Callable[..., Any]
    create_policy: Callable[..., Any]
    create_cost_model: Callable[..., Any]
    recognize: Callable[..., dict]
    complexity: Callable[..., dict]
    proxy: Callable[..., float]
    proxy_mode: str
    proxy_note: str

    def __post_init__(self):
        _callables(self, ('create_estimator', 'create_policy', 'create_cost_model',
                         'recognize', 'complexity', 'proxy'))
        if not isinstance(self.proxy_mode, str) or not isinstance(self.proxy_note, str):
            raise TypeError('prediction proxy labels must be strings')


@dataclass(frozen=True)
class ProposalSet:
    seed: Callable[..., Any]
    create_constructor: Callable[..., Any]
    initial: Callable[..., Any]
    local: Callable[..., Any]
    rebuild: Callable[..., Any]
    schedule: Callable[..., Any]
    retain: Callable[..., Any]
    control_profile: Callable[..., dict]
    controlled_parameters: tuple[str, ...] = ()

    def __post_init__(self):
        _callables(self, ('seed', 'create_constructor', 'initial', 'local', 'rebuild',
                         'schedule', 'retain', 'control_profile'))
        if (not isinstance(self.controlled_parameters, tuple) or
                any(not isinstance(name, str) for name in self.controlled_parameters)):
            raise TypeError('controlled_parameters must be a tuple of parameter names')


@dataclass(frozen=True)
class CaseRuntime:
    """Optional case-wide policy; common API still owns the hard supervisor.

    worker(request, output, started, single, problem) writes checkpoints;
    recover(request, output, started, single, expired, telemetry) validates and
    publishes those checkpoints after normal completion or interruption.
    """
    worker: Callable[..., Any]
    recover: Callable[..., dict]

    def __post_init__(self):
        _callables(self, ('worker', 'recover'))


@dataclass(frozen=True)
class ProblemDefinition:
    id: str
    version: str
    semantics: PlanSemantics
    execution: ExecutionModel
    prediction: PredictionModel
    proposals: ProposalSet
    default_parameters: Callable[[], dict]
    supported_cores: tuple[int, ...] = (2, 3, 4, 5)
    artifact_files: tuple[str, ...] = ()
    case_runtime: CaseRuntime | None = None

    def __post_init__(self):
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError('problem id must be a nonempty string')
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError('problem version must be a nonempty string')
        for name, kind in (('semantics', PlanSemantics), ('execution', ExecutionModel),
                           ('prediction', PredictionModel), ('proposals', ProposalSet)):
            if not isinstance(getattr(self, name), kind):
                raise TypeError(f'{name} must be a complete {kind.__name__}')
        _callables(self, ('default_parameters',))
        if self.case_runtime is not None and not isinstance(self.case_runtime, CaseRuntime):
            raise TypeError('case_runtime must be a complete CaseRuntime')
        if (not isinstance(self.supported_cores, tuple) or not self.supported_cores or
                len(set(self.supported_cores)) != len(self.supported_cores) or
                any(type(n) is not int or n < 1 for n in self.supported_cores)):
            raise ValueError('supported_cores must contain distinct positive integers')
        if (not isinstance(self.artifact_files, tuple) or
                any(not isinstance(path, str) for path in self.artifact_files)):
            raise TypeError('artifact_files must be a tuple of paths')

    def metadata(self):
        return dict(problem_id=self.id, backend_version=self.version)
