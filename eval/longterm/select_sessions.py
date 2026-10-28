#!/usr/bin/env python3
"""
从 session-archive 的文件树里抽样 Claude Code / Codex 会话（可复现）

    gh api "repos/OWNER/session-archive/git/trees/COMMIT?recursive=1" > tree.json
    python3 eval/longterm/select_sessions.py tree.json > pick.txt
    git -C session-archive sparse-checkout set --no-cone --stdin < <(echo /openclaw-sessions/; cat pick.txt)

规则：Claude Code 每个项目目录最多 3 个主会话（不含子代理，20KB–3MB）；
Codex 每个月最多 3 个会话（20KB–20MB）。同一棵树 + 同一个种子 → 同一批文件。
"""

import argparse
import json
import random
import re
from collections import defaultdict


def select(tree, seed=11):
    blobs = [x for x in tree["tree"] if x["type"] == "blob"]
    rng = random.Random(seed)

    cc = [
        x
        for x in blobs
        if x["path"].startswith("claude-code-sessions/")
        and "/subagents/" not in x["path"]
        and x["path"].endswith(".jsonl")
        and 20e3 <= x["size"] <= 3e6
    ]
    by_project = defaultdict(list)
    for x in cc:
        by_project["/".join(x["path"].split("/")[:3])].append(x)
    picked = [y for v in by_project.values() for y in rng.sample(v, min(3, len(v)))]

    cx = [
        x
        for x in blobs
        if x["path"].startswith("codex-sessions/") and re.search(r"/\d{4}/\d{2}/\d{2}/", x["path"]) and 20e3 <= x["size"] <= 20e6
    ]
    by_month = defaultdict(list)
    for x in cx:
        by_month[re.search(r"/(\d{4}/\d{2})/", x["path"]).group(1)].append(x)
    picked += [y for v in by_month.values() for y in rng.sample(v, min(3, len(v)))]
    return ["/" + x["path"] for x in picked]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("tree_json")
    ap.add_argument("--seed", type=int, default=11)
    args = ap.parse_args()
    print("\n".join(select(json.load(open(args.tree_json, encoding="utf-8")), args.seed)))
