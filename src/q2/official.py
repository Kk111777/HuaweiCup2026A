"""原版 Scene B 的唯一装载入口；版本、路径和硬件配置均显式核验。"""
from dataclasses import dataclass
from pathlib import Path
import hashlib
import importlib
import json
import sys
from common.artifacts import file_hash

ROOT = Path(__file__).resolve().parents[2]


class Q2Error(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def read_json(path):
    def unique(pairs):
        result = {}
        for k, v in pairs:
            if k in result:
                raise Q2Error('INPUT_INVALID', f'duplicate JSON key: {k}')
            result[k] = v
        return result
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=unique)
    except json.JSONDecodeError as error:
        raise Q2Error('INPUT_INVALID', str(error)) from error


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class Settings:
    root: Path
    config: Path
    capacity: dict
    bandwidth: int
    delay: int
    hashes: dict

    @classmethod
    def read(cls, reference=ROOT/'official', config=None):
        root = Path(reference).resolve()
        config = Path(config).resolve() if config else root/'data/config.txt'
        expected = read_json(ROOT/'configs/q2/official_sha256.json')
        actual = {name: file_hash(root/name) if (root/name).is_file() else None
                  for name in expected}
        if actual != expected or not config.is_file() or file_hash(config) != expected['data/config.txt']:
            raise Q2Error('REFERENCE_MISMATCH', '官方源码或固定 config 与已审版本不一致')
        code = root/'code'
        names = [Path(p).stem for p in expected if p.endswith('.py')]
        for name in names:
            module = sys.modules.get(name)
            if module and Path(module.__file__).resolve() != code/(name+'.py'):
                raise Q2Error('REFERENCE_MISMATCH', f'混入另一 reference-root: {module.__file__}')
        if str(code) not in sys.path:
            sys.path.insert(0, str(code))
        validation = importlib.import_module('evaluation_validation')
        scene = importlib.import_module('multicore_cut_evaluate_problem_2')
        # 导入后复核，防止已装载模块的传递导入逃出 reference-root。
        for name in names:
            module = sys.modules.get(name)
            if module and Path(module.__file__).resolve() != code/(name+'.py'):
                raise Q2Error('REFERENCE_MISMATCH', name)
        common = validation.read_evaluation_config(config)
        return cls(root, config, common['capacity'], common['bandwidth'],
                   scene.read_scene_b_config(config)['cross_core_copy_delay_cycles'], actual)

    def prefix(self, raw, plan):
        from .prefix import build_scene_b_prefix
        return build_scene_b_prefix(raw, plan, self.bandwidth, self.capacity)

    def evaluate(self, raw, plan):
        from multicore_cut_evaluate_problem_2 import evaluate_scene_b
        return evaluate_scene_b(raw, plan, self.bandwidth, self.capacity, self.delay)


def signature(plan):
    return (tuple(sorted((int(v), j) for v, j in plan['node_to_subgraph'].items())),
            tuple(tuple(q) for q in plan['core_schedules']))


def objective(result):
    return result['makespan'], result['data_movement_bytes']['added_copy_bytes']


def normalized(result):
    return json.loads(json.dumps({k: v for k, v in result.items()
                                 if k not in ('input_graph', 'input_plan')}))
