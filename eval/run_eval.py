#!/usr/bin/env python3
"""
中文记忆评测集运行器

按用例回放多天对话（用 MEMORY_NOW 固定"现在"），每天结束跑一次 consolidate，
然后检查两件事：
1. 记忆状态：该记住的在活跃池里、不该记的不在、同一件事不重复
2. 检索结果：Agent 用 search --json 能不能查到对的、会不会查到已过时的

用法：
    python3 eval/run_eval.py                    # 默认 pending 模式（add-pending + consolidate）
    python3 eval/run_eval.py --mode capture     # 每句话都 capture（模拟 Agent 主动写入）
    python3 eval/run_eval.py -v                 # 打印每条失败断言及当时的活跃记忆
    python3 eval/run_eval.py --only correction  # 只跑 id 以此开头的用例
    python3 eval/run_eval.py --json out.json    # 保存逐条结果
    python3 eval/run_eval.py --llm              # 启用 LLM 写入判断（需 OPENAI_API_KEY 等，见 src/llm_judge.py）

用例格式见 eval/README.md。
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MEMORY_PY = REPO_ROOT / "src" / "memory.py"
CASES_DIR = Path(__file__).resolve().parent / "cases"

# 断言类型 → 报告里的中文名
METRICS = {
    "remember": "该记的记住了",
    "forget": "不该记的没记",
    "unique": "不重复存储",
    "hit": f"检索命中@k",
    "top1": "检索首位正确",
    "stale": "不返回过时信息",
}


# 由 --llm 决定:默认去掉 API Key,只评测规则(+可选语义向量)路径,结果可复现
LLM_ENV = {}


def run_cli(memory_dir, now, *args):
    env = {**os.environ, "MEMORY_DIR": str(memory_dir), "MEMORY_NOW": now, **LLM_ENV}
    if env.get("MEMORY_LLM_JUDGE") != "on":
        env.pop("OPENAI_API_KEY", None)
    proc = subprocess.run(
        [sys.executable, str(MEMORY_PY), *args], env=env, capture_output=True, text=True, timeout=120
    )
    if proc.returncode != 0:
        raise RuntimeError(f"memory.py {' '.join(args)} 失败:\n{proc.stderr[-2000:]}")
    return proc.stdout


def load_pool(memory_dir, pool):
    records = []
    for mem_type in ["facts", "beliefs", "summaries"]:
        path = memory_dir / f"layer2/{pool}/{mem_type}.jsonl"
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    records.append(json.loads(line))
    return records


def matches(content, keywords):
    content = content.lower()
    return all(k.lower() in content for k in keywords)


def check_state(expect, active):
    """返回 [(metric, passed, detail)]"""
    out = []
    contents = [r["content"] for r in active]
    for kws in expect.get("remember", []):
        out.append(("remember", any(matches(c, kws) for c in contents), f"应记住 {kws}"))
    for kws in expect.get("forget", []):
        hits = [c for c in contents if matches(c, kws)]
        out.append(("forget", not hits, f"不应记住 {kws}" + (f"，实际有: {hits}" if hits else "")))
    for kws in expect.get("unique", []):
        hits = [c for c in contents if matches(c, kws)]
        out.append(("unique", len(hits) <= 1, f"{kws} 最多 1 条，实际 {len(hits)} 条" + (f": {hits}" if len(hits) > 1 else "")))
    return out


def check_query(query, memory_dir, now, k, by_id):
    raw = run_cli(memory_dir, now, "search", query["q"], "--json", "--limit", str(k))
    result = json.loads(raw)
    ranked = [by_id.get(r["id"], r["content"]) for r in result["results"]][:k]
    shown = f"查询「{query['q']}」→ {ranked}"
    out = []
    hit_kws = query.get("hit")
    if hit_kws:
        out.append(("hit", any(matches(c, hit_kws) for c in ranked), f"{shown} 应包含 {hit_kws}"))
    if query.get("top1"):
        out.append(("top1", bool(ranked) and matches(ranked[0], query["top1"]), f"{shown} 首位应包含 {query['top1']}"))
    if query.get("stale"):
        # 包含过时关键词、又不包含正确关键词的结果视为过时信息
        stale = [c for c in ranked if matches(c, query["stale"]) and not (hit_kws and matches(c, hit_kws))]
        out.append(("stale", not stale, f"{shown} 不应出现过时信息 {query['stale']}"))
    return out


def run_case(case, mode, k):
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        memory_dir = Path(tmp) / "memory"
        first = case["sessions"][0]["date"]
        run_cli(memory_dir, f"{first}T08:00:00Z", "init")
        for day, session in enumerate(case["sessions"], 1):
            now = f"{session['date']}T10:00:00Z"
            for msg in session.get("say", []):
                if mode == "capture":
                    run_cli(memory_dir, now, "capture", msg)
                else:
                    run_cli(memory_dir, now, "add-pending", msg)
            run_cli(memory_dir, f"{session['date']}T23:00:00Z", "consolidate", "--force")

            check_now = f"{session['date']}T23:30:00Z"
            active = load_pool(memory_dir, "active")
            by_id = {r["id"]: r["content"] for r in active + load_pool(memory_dir, "archive")}
            checks = check_state(session.get("expect", {}), active)
            for query in session.get("queries", []):
                checks += check_query(query, memory_dir, check_now, k, by_id)
            for metric, passed, detail in checks:
                results.append(
                    {
                        "case": case["id"],
                        "category": case["category"],
                        "day": day,
                        "metric": metric,
                        "passed": passed,
                        "detail": detail,
                        "active": [r["content"] for r in active],
                    }
                )
    return results


def load_cases(only=None):
    cases = []
    for path in sorted(CASES_DIR.glob("*.json")):
        for case in json.loads(path.read_text(encoding="utf-8")):
            case.setdefault("category", path.stem)
            if not only or case["id"].startswith(only):
                cases.append(case)
    ids = [c["id"] for c in cases]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        sys.exit(f"用例 id 重复: {dup}")
    return cases


def pct(passed, total):
    return f"{passed}/{total} ({passed / total:.0%})" if total else "-"


def report(results, verbose):
    by_cat = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    overall = defaultdict(lambda: [0, 0])
    for r in results:
        for bucket in (by_cat[r["category"]][r["metric"]], overall[r["metric"]]):
            bucket[0] += r["passed"]
            bucket[1] += 1

    metrics = [m for m in METRICS if overall[m][1]]
    header = ["类别"] + [METRICS[m] for m in metrics] + ["合计"]
    rows = []
    for cat in sorted(by_cat):
        cells = [cat]
        p = t = 0
        for m in metrics:
            bp, bt = by_cat[cat][m]
            cells.append(pct(bp, bt))
            p, t = p + bp, t + bt
        cells.append(pct(p, t))
        rows.append(cells)
    total_p = sum(v[0] for v in overall.values())
    total_t = sum(v[1] for v in overall.values())
    rows.append(["**总计**"] + [pct(*overall[m]) for m in metrics] + [pct(total_p, total_t)])

    print("| " + " | ".join(header) + " |")
    print("|" + "|".join(["---"] * len(header)) + "|")
    for row in rows:
        print("| " + " | ".join(row) + " |")

    failed = [r for r in results if not r["passed"]]
    print(f"\n失败断言: {len(failed)} 条（涉及 {len({r['case'] for r in failed})} 个用例）")
    if verbose:
        for r in failed:
            print(f"\n❌ [{r['case']} 第{r['day']}天] {METRICS[r['metric']]}: {r['detail']}")
            print(f"   活跃记忆: {r['active']}")
    return total_p, total_t


def main():
    parser = argparse.ArgumentParser(description="中文记忆评测")
    parser.add_argument("--mode", choices=["pending", "capture"], default="pending")
    parser.add_argument("--k", type=int, default=3, help="检索命中看前 k 条")
    parser.add_argument("--only", help="只跑 id 以此开头的用例")
    parser.add_argument("--json", help="逐条结果写入该文件")
    parser.add_argument("-j", "--jobs", type=int, default=8)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--llm", action="store_true", help="启用 LLM 写入判断(关闭旧的 LLM 兜底,只评测判断本身)")
    args = parser.parse_args()

    LLM_ENV.update(
        {"MEMORY_LLM_JUDGE": "on", "MEMORY_LLM_ENABLED": "false"} if args.llm else {"MEMORY_LLM_JUDGE": "off"}
    )

    cases = load_cases(args.only)
    semantic = os.environ.get("MEMORY_EMBEDDING_PROVIDER", "none")
    print(f"运行 {len(cases)} 个用例（模式: {args.mode}, k={args.k}, 语义向量: {semantic}, LLM 判断: {'开' if args.llm else '关'}）\n")
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        per_case = list(pool.map(lambda c: run_case(c, args.mode, args.k), cases))
    results = [r for rs in per_case for r in rs]

    report(results, args.verbose)
    if args.json:
        Path(args.json).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
