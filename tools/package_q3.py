"""Build a self-contained local Q3 source/data package, without Git mutation."""
import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--include-inputs', action='store_true')
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output already exists; choose a new path')
    paths = set()
    for folder, pattern in [('src', '*.py'), ('tests', '*.py'), ('tests/fixtures', '*.json'),
                            ('configs', '*.json'), ('official/code', '*.py'), ('docs/q3', '*.md')]:
        paths.update((ROOT / folder).rglob(pattern))
    paths.update(ROOT / name for name in ('README_Q3.md', 'requirements-q3.txt',
        'official/data/config.txt', 'tools/package_q3.py', 'tools/audit_q3_results.py'))
    for folder in ('results/q2_repair_full_20260926', 'results/problem_1_singlecore'):
        paths.update(p for p in (ROOT / folder).rglob('*') if p.is_file())
    if args.include_inputs:
        paths.update((ROOT / 'official/data').glob('case_*.json'))
    manifest = {}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(args.output, 'x', ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(paths):
            name = path.relative_to(ROOT).as_posix()
            content = path.read_bytes()
            manifest[name] = {'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content)}
            archive.writestr('Q3_reproduction/' + name, content)
        archive.writestr('Q3_reproduction/package_manifest.json',
            json.dumps({'format': 1, 'files': manifest, 'python': '3.12',
                        'entrypoint': 'src/solve_problem3.py', 'input_cases': sum(n.startswith('official/data/case_') for n in manifest)},
                       indent=2, ensure_ascii=False) + '\n')
    with ZipFile(args.output) as archive:
        if archive.testzip() is not None:
            raise RuntimeError('ZIP CRC check failed')
        for name, record in manifest.items():
            if hashlib.sha256(archive.read('Q3_reproduction/' + name)).hexdigest() != record['sha256']:
                raise RuntimeError('package content hash mismatch: ' + name)
    digest = hashlib.sha256(args.output.read_bytes()).hexdigest()
    args.output.with_suffix('.sha256').write_text(f'{digest}  {args.output.name}\n', encoding='utf-8')
    print(json.dumps(dict(path=str(args.output.resolve()), files=len(manifest), bytes=args.output.stat().st_size,
                         sha256=digest), ensure_ascii=False))


if __name__ == '__main__':
    main()
