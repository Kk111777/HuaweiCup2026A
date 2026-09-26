"""Archive one complete Q2 V3 search and its independent official replay."""

import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
import shutil
import statistics
import sys
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from common.artifacts import file_hash
from q2.optimized.api import frozen_output


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def csv_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def write_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def aggregate(rows):
    output = {
        "groups": len(rows),
        "mean_speedup": statistics.mean(row["speedup"] for row in rows),
        "search_complete": sum(row["search_complete"] for row in rows),
        "baseline_guard_complete": sum(row["baseline_guard_complete"] for row in rows),
    }
    for name in ("v2", "q1v3"):
        output[f"{name}_mean_speedup"] = statistics.mean(row[f"{name}_speedup"] for row in rows)
        output[f"vs_{name}"] = {
            "wins": sum(row["official_makespan_cycles"] < row[f"{name}_cycles"] for row in rows),
            "ties": sum(row["official_makespan_cycles"] == row[f"{name}_cycles"] for row in rows),
            "losses": sum(row["official_makespan_cycles"] > row[f"{name}_cycles"] for row in rows),
        }
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--search", type=Path, required=True)
    parser.add_argument("--replay", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    search, replay, output = (p.resolve() for p in (args.search, args.replay, args.output))
    require(not output.exists(), "output already exists; keep prior archives immutable")

    request = read(search / "batch_request.json")
    batch = read(search / "batch_summary.json")
    replay_batch = read(replay / "replay_summary.json")
    segments = read(search / "execution_segments.json")
    require(request["algorithm_version"] == batch["algorithm_version"] == "Q2-Optimized-3.0",
            "unexpected algorithm version")
    require(request["cores"] == [2, 3, 4, 5] and request["seconds"] == 600,
            "unexpected search coverage or time target")
    require(batch["requested"] == batch["finished"] == batch["successful"] == 100,
            "search batch is incomplete")
    require(replay_batch["requested"] == replay_batch["finished"] == 400
            and replay_batch["counts"] == {"OFFICIAL_REPLAY_OK": 400},
            "independent replay is incomplete")
    require(file_hash(ROOT / "official/data/config.txt") == request["config_sha256"],
            "official config changed")

    v2_path = ROOT / "results/q2_repair_full_20260926/details.csv"
    q1_path = ROOT / "results/q1_structure_100cases_20260926/official_summary.csv"
    v2 = {(row["case"], int(row["num_cores"])): row for row in csv_rows(v2_path)}
    q1 = {(row["case"], int(row["cores"])): row for row in csv_rows(q1_path)}
    require(len(v2) == len(q1) == 400, "historical comparison coverage differs")
    require(read(ROOT / "results/q2_repair_full_20260926/provenance.json")["algorithm"]["algorithm_version"]
            == "Q2-Repair-2.0", "historical V2 version differs")

    output.mkdir(parents=True)
    (output / "input_sha256.txt").write_text(
        "".join(f"{request['graphs'][case]}  {case}.json\n" for case in sorted(request["graphs"])),
        encoding="utf-8")
    for name in ("batch_request.json", "batch_summary.json", "execution_segments.json"):
        shutil.copyfile(search / name, output / name)
    shutil.copyfile(replay / "replay_summary.json", output / "replay_summary.json")

    details, comparison, artifact_hashes = [], [], []
    cases = sorted(request["graphs"])
    require(cases == [f"case_{i:03d}" for i in range(1, 101)], "expected exactly case_001..case_100")
    with ZipFile(output / "plans.zip", "x", compression=ZIP_DEFLATED, compresslevel=6) as archive:
        for case in cases:
            case_request = read(search / case / "request.json")
            graph = Path(case_request["graph"])
            require(file_hash(graph) == request["graphs"][case], f"input changed: {case}")
            result = batch["results"][case]
            require(result["status"] == "ok" and result["coverage_complete"]
                    and [r["cores"] for r in result["rows"]] == [2, 3, 4, 5],
                    f"incomplete case: {case}")
            for row in result["rows"]:
                cores = row["cores"]
                key = (case, cores)
                frozen = search / case / f"N{cores}"
                replayed = replay / case / f"N{cores}"
                manifest, state = frozen_output(frozen)
                verified = read(replayed / "verified.json")
                replay_record = replay_batch["results"][f"{case}/N{cores}"]
                require(row["status"] == "SUCCESS_EXACT_UNREPLAYED"
                        and verified["status"] == replay_record["status"] == "OFFICIAL_REPLAY_OK"
                        and verified["independent_replay"] and not verified["included_in_search_budget"],
                        f"score provenance differs: {case}/N{cores}")
                plan_sha = file_hash(frozen / "plan.json")
                result_sha = file_hash(replayed / "result.json")
                require(verified["frozen_plan_sha256"] == plan_sha
                        and verified["result_sha256"] == result_sha,
                        f"replay artifacts changed: {case}/N{cores}")
                cycles = verified["official_makespan"]
                movement = verified["data_movement_bytes"]
                require(cycles == row["makespan"] == state.score.makespan
                        and movement["added_copy_bytes"] == row["added_copy_bytes"] == state.score.added_copy_bytes
                        and verified["prediction_error_cycles"] == 0,
                        f"official replay differs from search: {case}/N{cores}")
                require(v2[key]["status"] == "SUCCESS_VERIFIED" and q1[key]["validation_status"] == "ok",
                        f"historical result unverified: {case}/N{cores}")
                baseline = int(v2[key]["singlecore_cycles"])
                require(baseline == int(q1[key]["baseline_cycles"]),
                        f"single-core denominator differs: {case}/N{cores}")

                member = f"{case}/N{cores}/plan.json"
                info = ZipInfo(member, date_time=(2026, 9, 27, 0, 0, 0))
                info.compress_type = ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                archive.writestr(info, (frozen / "plan.json").read_bytes(), compresslevel=6)
                for source, directory, filenames in (
                    ("search", frozen, ("plan.json", "state.json", "score.json", "legality.json", "frozen_manifest.json")),
                    ("replay", replayed, ("result.json", "trace.json", "verified.json")),
                ):
                    for filename in filenames:
                        path = directory / filename
                        artifact_hashes.append({
                            "source": source,
                            "relative_path": f"{case}/N{cores}/{filename}",
                            "bytes": path.stat().st_size,
                            "sha256": file_hash(path),
                            "published": filename == "plan.json",
                        })
                record = {
                    "case": case,
                    "cores": cores,
                    "validation_status": "OFFICIAL_REPLAY_OK",
                    "singlecore_cycles": baseline,
                    "official_makespan_cycles": cycles,
                    "speedup": baseline / cycles,
                    "added_copy_bytes": movement["added_copy_bytes"],
                    "partition_added_copy_bytes": movement["partition_added_copy_bytes"],
                    "spill_added_copy_bytes": movement["spill_added_copy_bytes"],
                    "search_complete": row["search_complete"],
                    "search_stop_reason": row["stop_reason"],
                    "baseline_guard_complete": row["baseline_guard"]["complete"],
                    "baseline_guard_cycles": row["baseline_guard"]["baseline_makespan"],
                    "case_search_wall_seconds": result["wall_seconds"],
                    "core_search_wall_seconds": row["timing"]["wall_seconds"],
                    "official_replay_wall_seconds": sum(call["wall_seconds"] for call in verified["calls"]),
                    "plan_zip_member": member,
                    "plan_sha256": plan_sha,
                    "official_result_sha256": result_sha,
                    "official_trace_sha256": file_hash(replayed / "trace.json"),
                    "input_sha256": request["graphs"][case],
                }
                details.append(record)
                v2_cycles = int(v2[key]["makespan_cycles"])
                q1_cycles = int(q1[key]["official_makespan_cycles"])
                comparison.append({
                    "case": case, "cores": cores, "singlecore_cycles": baseline,
                    "q2_v3_cycles": cycles, "q2_v3_speedup": baseline / cycles,
                    "q2_v2_historical_cycles": v2_cycles,
                    "q2_v2_historical_speedup": baseline / v2_cycles,
                    "q1v3_cycles": q1_cycles, "q1v3_speedup": baseline / q1_cycles,
                    "q2_v3_vs_v2_historical": "win" if cycles < v2_cycles else "tie" if cycles == v2_cycles else "loss",
                    "q2_v3_vs_q1v3": "win" if cycles < q1_cycles else "tie" if cycles == q1_cycles else "loss",
                    "baseline_guard_complete": row["baseline_guard"]["complete"],
                })

    require(len(details) == len(comparison) == 400 and len(artifact_hashes) == 3200,
            "archive coverage differs")
    write_csv(output / "details.csv", details)
    write_csv(output / "comparison.csv", comparison)
    write_csv(output / "artifact_hashes.csv", artifact_hashes)

    groups = []
    for row in comparison:
        groups.append({
            "cores": row["cores"],
            "official_makespan_cycles": row["q2_v3_cycles"],
            "speedup": row["q2_v3_speedup"],
            "v2_cycles": row["q2_v2_historical_cycles"],
            "v2_speedup": row["q2_v2_historical_speedup"],
            "q1v3_cycles": row["q1v3_cycles"],
            "q1v3_speedup": row["q1v3_speedup"],
            "search_complete": details[len(groups)]["search_complete"],
            "baseline_guard_complete": details[len(groups)]["baseline_guard_complete"],
        })
    overall = aggregate(groups)
    per_core = {str(n): aggregate([row for row in groups if row["cores"] == n]) for n in (2, 3, 4, 5)}
    budget = {
        "target_seconds_per_case_all_cores": request["seconds"],
        "conditional_extension_seconds": request["seconds"] * 1.5,
        "hard_protection_seconds": request["seconds"] * 2,
        "mean_case_wall_seconds": statistics.mean(result["wall_seconds"] for result in batch["results"].values()),
        "max_case_wall_seconds": max(result["wall_seconds"] for result in batch["results"].values()),
        "cases_exceeded_target": sum(result["exceeded_target"] for result in batch["results"].values()),
        "cases_exceeded_extension": sum(result["exceeded_extension"] for result in batch["results"].values()),
        "cases_extended": sum(result["extended"] for result in batch["results"].values()),
        "cases_hard_timeout": sum(result["hard_timeout"] for result in batch["results"].values()),
    }
    summary = {
        "algorithm_version": "Q2-Optimized-3.0",
        "source_commit": args.source_commit,
        "cases": 100,
        "groups": 400,
        "official_independent_replays_ok": 400,
        "exact_search_replay_mismatches": 0,
        "overall": overall,
        "per_core": per_core,
        "search_stop_reasons": dict(Counter(row["search_stop_reason"] for row in details)),
        "budget": budget,
        "comparison_scope": "Same inputs, simulated core counts, fixed single-core denominators and official evaluator; historical V2 has a separate 600 s budget per case/core, while V3 shares 600 s per case. Not a matched-time comparison.",
    }
    write(output / "summary.json", summary)

    provenance = {
        "algorithm_version": request["algorithm_version"],
        "source_commit": args.source_commit,
        "source_signature": request["source_signature"],
        "requested_cores": request["cores"],
        "target_seconds": request["seconds"],
        "budget_scope": "case_all_cores",
        "parameters": request["parameters"],
        "final_batch_workers": request["workers"],
        "execution_segments": segments,
        "config_sha256": request["config_sha256"],
        "official_hash_manifest_sha256": file_hash(ROOT / "configs/q2/official_sha256.json"),
        "input_sha256": request["graphs"],
        "historical_comparators": {
            "q2_v2": {"algorithm_version": "Q2-Repair-2.0", "path": "results/q2_repair_full_20260926/details.csv",
                      "sha256": file_hash(v2_path), "budget_scope": "600_seconds_per_case_core"},
            "q1v3": {"path": "results/q1_structure_100cases_20260926/official_summary.csv",
                     "sha256": file_hash(q1_path)},
        },
        "published_inputs": False,
        "published_official_result_and_trace_files": False,
    }
    write(output / "provenance.json", provenance)

    lines = [
        "# 第二问 V3：100 图全量官方结果", "",
        "本目录归档 `Q2-Optimized-3.0` 在官方 100 张图、2/3/4/5 核上的完整本地实验。",
        "**400/400 份冻结方案均通过独立官方复评**，复评周期及新增搬运量与搜索内精确记录逐组一致。",
        f"运行源码为提交 [`{args.source_commit[:7]}`](https://github.com/Kk111777/HuaweiCup2026A/commit/{args.source_commit})；归档不修改求解器。", "",
        "| 模拟核数 | V3 平均加速比 | 历史 V2.0 | Q1v3 |", "| ---: | ---: | ---: | ---: |",
    ]
    for n in (2, 3, 4, 5):
        a = per_core[str(n)]
        lines.append(f"| {n} | {a['mean_speedup']:.6f} | {a['v2_mean_speedup']:.6f} | {a['q1v3_mean_speedup']:.6f} |")
    lines += [
        "",
        "加速比为同图固定单核官方周期数除以本组独立复评周期数，再对 100 张图取算术平均。",
        "输入、核数、单核分母和官方评分程序一致。**历史 V2.0 每张图每种核数各有 600 秒预算；本轮 V3 每张图四种核数合计共享 600 秒目标，近期改进可延至 900 秒，已启动工作由 1200 秒保护。**",
        "因此表内 V2 数字仅是历史成绩对照，不能解释为同时间预算下的算法增益；目前的 V2.1 接口版也没有在此重跑全量。",
        f"V3 对历史 V2.0 为 {overall['vs_v2']['wins']} 胜、{overall['vs_v2']['ties']} 平、{overall['vs_v2']['losses']} 负；对 Q1v3 为 {overall['vs_q1v3']['wins']} 胜、{overall['vs_q1v3']['ties']} 平、{overall['vs_q1v3']['losses']} 负。", "",
        "## 搜索与核验口径", "",
        f"100 张图均完成四种核数的方案输出；{overall['search_complete']}/400 组搜索耗尽提案，其余因时间窗口停止，不能称为搜索最优。",
        f"当次基座保底在 {overall['baseline_guard_complete']}/400 组完成；保底未完成的组不享有完整基座不退化保证。",
        f"整图平均实际搜索墙钟 {budget['mean_case_wall_seconds']:.2f} 秒，{budget['cases_extended']} 张图使用延长机制，0 张图触及硬保护超时。",
        "本批次曾调整外层并发并在机器重启后恢复；只复用源码、输入、参数指纹相同且产物完整的已完成图。最终批次参数为 6 路并发，历史段见 `execution_segments.json`。",
        "独立官方复评在搜索冻结后进行，不计入搜索墙钟；搜索内精确成绩与独立复评逐组一致。", "",
        "## 归档文件", "",
        "- `plans.zip`：400 份原样官方格式方案，成员为 `case_001/N2/plan.json` 等；不包含赛题输入。",
        "- `details.csv`：逐组正式周期、加速比、搬运量、搜索终止原因、墙钟与证据哈希。",
        "- `comparison.csv`：同图同核数的历史 V2.0 与 Q1v3 对照；预算不一致的限制如上。",
        "- `batch_request.json`、`batch_summary.json`、`replay_summary.json`：本次运行和独立复评的原始汇总。",
        "- `provenance.json`、`input_sha256.txt`：源码、配置、输入及对照来源指纹。",
        "- `artifact_hashes.csv`：本机 3,200 份冻结及复评产物的大小和 SHA-256。未上传的完整官方结果、Trace 和中间搜索文件不能仅凭此清单从仓库下载。",
        "- `archive_validation.json`、`manifest.sha256`：打包检查及发布文件校验值。", "",
        "## 复现与独立评分", "",
        "使用 Python 3.12，先准备官方 `case_001.json` 至 `case_100.json` 和 `official/data/config.txt`，并按 `input_sha256.txt` 核对输入。运行本次算法应检出上面的源码提交；墙钟截断及多进程争用会使重新搜索的方案可能不同。",
        "若只需复核已发布的方案，在仓库根目录运行：", "",
        "```bash",
        "python -m zipfile -e results/q2_optimized_full_20260927/plans.zip outputs/q2_v3_published_plans",
        "mkdir -p outputs/q2_v3_recheck_case001_N5",
        "python official/code/multicore_cut_evaluate_problem_2.py official/data/case_001.json \\",
        "  outputs/q2_v3_published_plans/case_001/N5/plan.json --config official/data/config.txt \\",
        "  -o outputs/q2_v3_recheck_case001_N5/result.json",
        "```", "",
        "对其余 399 组按相同格式替换图、核数和输出目录；将官方 `makespan` 与 `details.csv` 对照。", "",
    ]
    (output / "README.md").write_text("\n".join(lines), encoding="utf-8")

    with ZipFile(output / "plans.zip") as archive:
        require(archive.testzip() is None, "plan archive CRC failed")
        require(archive.namelist() == [row["plan_zip_member"] for row in details],
                "plan archive members differ")
        for row in details:
            require(hashlib.sha256(archive.read(row["plan_zip_member"])).hexdigest() == row["plan_sha256"],
                    f"archived plan changed: {row['case']}/N{row['cores']}")
    validation = {
        "status": "PASSED",
        "cases": len(cases),
        "groups": len(details),
        "independent_official_replays_verified": len(details),
        "exact_score_mismatches": 0,
        "official_added_copy_mismatches": 0,
        "local_artifacts_hashed": len(artifact_hashes),
        "local_artifact_bytes_hashed": sum(row["bytes"] for row in artifact_hashes),
        "plan_zip_members_rechecked": len(details),
        "plans_zip_sha256": file_hash(output / "plans.zip"),
    }
    write(output / "archive_validation.json", validation)
    published = sorted(path for path in output.iterdir() if path.is_file() and path.name != "manifest.sha256")
    (output / "manifest.sha256").write_text(
        "".join(f"{file_hash(path)}  {path.name}\n" for path in published), encoding="utf-8")
    print(json.dumps({"output": str(output), "cases": len(cases), "groups": len(details),
                      "plan_zip_bytes": (output / "plans.zip").stat().st_size,
                      "published_files": len(published) + 1}, ensure_ascii=False))


if __name__ == "__main__":
    main()
