#!/usr/bin/env python3
"""
失败归因：答错的题卡在哪个环节

    python3 eval/longterm/diagnose.py RESULTS.json [--configs rule,semantic,semantic_judge]

对每道答错的题（用 LLM 判断）：
- 编造        "没说过"的题给出了答案
- 答题失败    检索到的记忆里已有答案所需信息，LLM 没答对
- 检索未命中  检索结果里没有，但记忆库（活跃池）里有
- 没存进去    记忆库里也没有（被过滤 / 误合并 / 只出现在 AI 的回复里）
"""

import argparse
import json
import re
import sys
import tempfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_longterm import LLM, load_pool  # noqa: E402

CHECK_SYSTEM = "你判断一段记忆里有没有回答问题所需的信息。只输出 JSON。"
CHECK_PROMPT = """{memories}

问题：{q}
标准答案：{gold}

上面的记忆里，是否包含得出这个标准答案所需的关键信息（不要求措辞一致）？
只输出 JSON：{{"contains": true 或 false}}"""


def contains(llm, memories_text, q):
    text = llm(CHECK_SYSTEM, CHECK_PROMPT.format(memories=memories_text, q=q["q"], gold=q["answer"]), max_tokens=512)
    m = re.search(r"\{.*\}", text, re.S)
    try:
        return bool(json.loads(m.group(0))["contains"]) if m else False
    except ValueError:
        return False


def pool_text(memory_dir):
    lines = [f"- （{(r.get('valid_at') or r.get('created') or '')[:10]}）{r['content']}" for r in load_pool(Path(memory_dir), "active")]
    return "【记忆库全部内容】\n" + "\n".join(lines)


def diagnose(llm, r, pools):
    if r["category"] == "abstention":
        return "编造"
    if contains(llm, r["retrieved"] or "", r):
        return "答题失败"
    pool = pools.get(r["timeline"])
    if pool and contains(llm, pool, r):
        return "检索未命中"
    return "没存进去"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("--configs", default="rule,semantic,semantic_judge")
    ap.add_argument("--model", default="deepseek-flash")
    ap.add_argument("--cache", default=str(Path(tempfile.gettempdir()) / "longterm-llm-cache"))
    ap.add_argument("--out", help="逐题归因 JSON")
    args = ap.parse_args()

    data = json.loads(Path(args.results).read_text(encoding="utf-8"))
    llm = LLM(args.model, Path(args.cache))
    table = defaultdict(Counter)
    detail = {}
    for config in args.configs.split(","):
        if config not in data["results"]:
            continue
        pools = {tl: pool_text(st["memory_dir"]) for tl, st in data["stats"].get(config, {}).items() if st.get("memory_dir")}
        wrong = [r for r in data["results"][config] if not r["correct"]]
        with ThreadPoolExecutor(max_workers=8) as ex:
            causes = list(ex.map(lambda r: diagnose(llm, r, pools), wrong))
        for r, c in zip(wrong, causes):
            table[(config, r["source"])][c] += 1
            table[(config, "全部")][c] += 1
        detail[config] = [{"timeline": r["timeline"], "q": r["q"], "category": r["category"], "cause": c} for r, c in zip(wrong, causes)]

    causes = ["没存进去", "检索未命中", "答题失败", "编造"]
    print("| 配置 | 数据集 | " + " | ".join(causes) + " | 答错合计 |")
    print("|" + "|".join(["---"] * (len(causes) + 3)) + "|")
    for (config, src), cnt in sorted(table.items(), key=lambda kv: (kv[0][0], kv[0][1] == "全部", kv[0][1])):
        print(f"| {config} | {src} | " + " | ".join(str(cnt.get(c, 0)) for c in causes) + f" | {sum(cnt.values())} |")
    if args.out:
        Path(args.out).write_text(json.dumps(detail, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
