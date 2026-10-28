#!/usr/bin/env python3
"""
长期记忆端到端评测：Agent 能不能像人一样记住过去的对话

流程（每条时间线）：
1. 按天回放对话：每句用户消息 add-pending，每天结束 consolidate（MEMORY_NOW 固定为当天）
2. 最后一天之后提问：Agent 只能看到 search --json 检索到的记忆（带日期），据此回答
3. LLM 按标准答案判对错；"没提过的事"答"不记得"才算对

对照：
- no_memory     不给任何记忆（下限：能靠常识猜对多少）
- full_context  把全部原始对话直接给 LLM（上限：完美记忆）
- rule / semantic / semantic_judge  本记忆系统的三种配置

用法：
    python3 eval/longterm/run_longterm.py --data DATA_DIR --configs rule,semantic --out results.json
LLM 接口用 OPENAI_API_KEY / OPENAI_BASE_URL，模型用 --model（默认 deepseek-flash）。
semantic 系列配置需要 MEMORY_EMBEDDING_* 环境变量（见 src/embeddings.py）。
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from adapters import LOADERS  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
MEMORY_PY = REPO_ROOT / "src" / "memory.py"
SEMANTIC_ENV = ("MEMORY_EMBEDDING_PROVIDER", "MEMORY_EMBEDDING_MODEL_DIR", "MEMORY_EMBEDDING_QUERY_PREFIX")

CONFIGS = {
    "no_memory": None,
    "full_context": None,
    "rule": {"semantic": False, "judge": False},
    "semantic": {"semantic": True, "judge": False},
    "semantic_judge": {"semantic": True, "judge": True},
}

# ============================================================
# LLM（OpenAI 兼容接口，带磁盘缓存）
# ============================================================


class LLM:
    def __init__(self, model, cache_dir: Path):
        self.model = model
        self.url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
        self.key = os.environ["OPENAI_API_KEY"]
        self.cache_dir = cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)

    def __call__(self, system, user, max_tokens=2048):
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        key = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        path = self.cache_dir / f"{key}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))["text"]
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.key}"},
        )
        last_error = None
        for _ in range(3):
            try:
                with urllib.request.urlopen(req, timeout=180) as resp:
                    msg = json.loads(resp.read().decode())["choices"][0]["message"]
                text = msg.get("content") or ""
                path.write_text(json.dumps({"text": text}, ensure_ascii=False), encoding="utf-8")
                return text
            except Exception as e:  # 网络抖动重试
                last_error = e
        raise RuntimeError(f"LLM 调用失败: {last_error}")


# ============================================================
# 回放：把对话写进记忆系统
# ============================================================


def cli(memory_dir, now, env_extra, *args):
    env = {**os.environ, "MEMORY_DIR": str(memory_dir), "MEMORY_NOW": now, **env_extra}
    proc = subprocess.run([sys.executable, str(MEMORY_PY), *args], env=env, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"memory.py {args[0]} 失败: {proc.stderr[-1500:]}")
    return proc.stdout


def config_env(cfg):
    env = {"MEMORY_LLM_ENABLED": "false", "MEMORY_LLM_JUDGE": "on" if cfg["judge"] else "off"}
    if not cfg["semantic"]:
        env.update({k: "" for k in SEMANTIC_ENV})
        env["MEMORY_EMBEDDING_PROVIDER"] = "none"
    return env


def replay(timeline, cfg, memory_dir, ingest_roles, archive_threshold=None):
    env = config_env(cfg)
    first = timeline["sessions"][0]["date"]
    cli(memory_dir, f"{first}T08:00:00Z", env, "init")
    if archive_threshold not in (None, 0):
        raise ValueError("衰减归档已移除；复现历史阈值实验请使用提交 04b164e")
    if archive_threshold is not None:
        # 仅兼容旧命令 --archive-threshold 0
        config_path = memory_dir / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["thresholds"]["archive"] = archive_threshold
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    for session in timeline["sessions"]:
        for i, turn in enumerate(session["turns"]):
            if turn["role"] in ingest_roles and turn["text"].strip():
                minute = min(i, 59 * 8)
                now = f"{session['date']}T{10 + minute // 60:02d}:{minute % 60:02d}:00Z"
                cli(memory_dir, now, env, "add-pending", turn["text"], "--source", turn["role"])
        cli(memory_dir, f"{session['date']}T23:00:00Z", env, "consolidate", "--force")


def load_pool(memory_dir, pool):
    out = []
    for t in ("facts", "beliefs", "summaries"):
        p = memory_dir / f"layer2/{pool}/{t}.jsonl"
        if p.exists():
            out += [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    return out


def retrieve(memory_dir, cfg, now, question, k):
    raw = cli(memory_dir, now, config_env(cfg), "search", question, "--json", "--limit", str(k))
    ids = [r["id"] for r in json.loads(raw)["results"]]
    records = {r["id"]: r for r in load_pool(memory_dir, "active")}
    return [records[i] for i in ids if i in records]


# ============================================================
# 回答与判分
# ============================================================

ANSWER_SYSTEM = (
    "你是用户长期相处的 AI 伙伴。只能根据下面提供的记忆或对话回答问题，不要编造。"
    "如果提供的内容里找不到答案，就回答“我不记得了”（英文问题回答 \"I don't remember\"）。"
    "用问题的语言简短作答。"
)


def answer_prompt(question, context, today):
    return f"今天是 {today}。\n\n{context}\n\n问题：{question}"


def memory_context(records):
    if not records:
        return "【你的记忆】\n（没有相关记忆）"
    lines = []
    for r in records:
        when = (r.get("event_date") or r.get("valid_at") or r.get("created") or "")[:10]
        lines.append(f"- （{when}）{r['content']}")
    return "【你的记忆】\n" + "\n".join(lines)


def transcript(timeline):
    parts = []
    for s in timeline["sessions"]:
        parts.append(f"## {s['date']}")
        for t in s["turns"]:
            who = "用户" if t["role"] == "user" else "AI"
            text = t["text"] if timeline["source"] == "locomo" else f"{who}（{t['speaker']}）：{t['text']}"
            parts.append(text)
    return "【完整对话记录】\n" + "\n".join(parts)


GRADE_SYSTEM = "你是严格但公正的评分员。只输出 JSON。"
GRADE_PROMPT = """问题：{q}
标准答案：{gold}
回答：{pred}

回答与标准答案意思一致（措辞、日期格式不同、多给了不矛盾的细节都算一致）则判对；答错、答不全关键点、或说不记得，判错。
只输出 JSON：{{"correct": true 或 false}}"""

ABSTAIN = re.compile(r"不记得|没有提到|没提过|不知道|无法确定|没有相关|don'?t (remember|know)|not mentioned|no information|cannot (be )?determine", re.I)


def grade(llm, q, pred):
    if q["category"] == "abstention":
        return bool(ABSTAIN.search(pred))
    if ABSTAIN.search(pred) and len(pred) < 40:
        return False
    text = llm(GRADE_SYSTEM, GRADE_PROMPT.format(q=q["q"], gold=q["answer"], pred=pred), max_tokens=512)
    m = re.search(r"\{.*\}", text, re.S)
    try:
        return bool(json.loads(m.group(0))["correct"]) if m else False
    except ValueError:
        return False


GOLD_SYSTEM = "根据完整对话记录，给出问题的标准答案：简短、具体，涉及日期时写明日期。若记录中确实没有答案，输出“无答案”。"


def ensure_gold(llm, timelines):
    """MemoryBank 没有标准答案：用完整对话生成一次（结果缓存）；生成不出答案的题不计分"""
    for tl in timelines:
        for q in tl["questions"]:
            if q["answer"] is None and q["category"] != "abstention":
                gold = llm(GOLD_SYSTEM, f"{transcript(tl)}\n\n问题：{q['q']}").strip()
                q["answer"] = None if "无答案" in gold else gold
                q["gold_generated"] = True


# ============================================================
# 主流程
# ============================================================


def run_timeline(tl, config, llm, k, ingest_roles, work_dir, archive_threshold=None):
    today = (date.fromisoformat(tl["sessions"][-1]["date"]) + timedelta(days=1)).isoformat()
    questions = [q for q in tl["questions"] if q["answer"] is not None or q["category"] == "abstention"]
    results = []
    stats = {}
    if config in ("no_memory", "full_context"):
        ctx = "【你的记忆】\n（没有相关记忆）" if config == "no_memory" else transcript(tl)
        contexts = [ctx] * len(questions)
    else:
        cfg = CONFIGS[config]
        mem = Path(tempfile.mkdtemp(dir=work_dir)) / "memory"
        replay(tl, cfg, mem, ingest_roles, archive_threshold)
        stats = {"stored": len(load_pool(mem, "active")), "archived": len(load_pool(mem, "archive")), "memory_dir": str(mem)}
        now = f"{today}T09:00:00Z"
        contexts = [memory_context(retrieve(mem, cfg, now, q["q"], k)) for q in questions]
    for q, ctx in zip(questions, contexts):
        pred = llm(ANSWER_SYSTEM, answer_prompt(q["q"], ctx, today), max_tokens=1024).strip()
        results.append({**q, "timeline": tl["id"], "source": tl["source"], "pred": pred, "correct": grade(llm, q, pred),
                        "retrieved": ctx if config not in ("no_memory", "full_context") else None})
    return results, stats


def report(all_results):
    configs = list(all_results)
    rows = defaultdict(dict)
    for config, res in all_results.items():
        groups = defaultdict(list)
        for r in res:
            groups[(r["source"], r["category"])].append(r["correct"])
            groups[(r["source"], "全部")].append(r["correct"])
            groups[("总计", "全部")].append(r["correct"])
        for key, vals in groups.items():
            rows[key][config] = f"{sum(vals)}/{len(vals)} ({sum(vals) / len(vals):.0%})"
    header = ["数据集", "类别"] + configs
    print("| " + " | ".join(header) + " |")
    print("|" + "|".join(["---"] * len(header)) + "|")
    for key in sorted(rows, key=lambda k: (k[0] == "总计", k[0], k[1] == "全部", k[1])):
        print("| " + " | ".join([key[0], key[1]] + [rows[key].get(c, "-") for c in configs]) + " |")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="fetch_datasets.sh 下载的数据目录")
    ap.add_argument("--timelines", help="额外的统一格式时间线 JSON（如私有数据集）")
    ap.add_argument("--sources", default="memorybank,perltqa,locomo")
    ap.add_argument("--configs", default="no_memory,full_context,rule,semantic,semantic_judge")
    ap.add_argument("--model", default="deepseek-flash")
    ap.add_argument("--k", type=int, default=10, help="每题检索的记忆条数")
    roles_group = ap.add_mutually_exclusive_group()
    roles_group.add_argument("--ingest-assistant", dest="ingest_assistant", action="store_true", help="写入用户和 AI 回复（默认）")
    roles_group.add_argument("--user-only", dest="ingest_assistant", action="store_false", help="只写用户消息，用于消融实验")
    ap.set_defaults(ingest_assistant=True)
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--archive-threshold", type=float, choices=[0], help="兼容旧命令，仅接受 0；默认不再衰减归档")
    ap.add_argument("--cache", default=str(Path(tempfile.gettempdir()) / "longterm-llm-cache"))
    ap.add_argument("--out", help="逐题结果 JSON")
    args = ap.parse_args()

    llm = LLM(args.model, Path(args.cache))
    timelines = []
    for src in [s for s in args.sources.split(",") if s]:
        timelines += LOADERS[src](Path(args.data))
    if args.timelines:
        timelines += json.loads(Path(args.timelines).read_text(encoding="utf-8"))
    ensure_gold(llm, timelines)

    roles = {"user", "assistant"} if args.ingest_assistant else {"user"}
    work_dir = Path(tempfile.mkdtemp(prefix="longterm-"))
    all_results, all_stats = {}, {}
    for config in args.configs.split(","):
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            outs = list(
                pool.map(lambda tl: run_timeline(tl, config, llm, args.k, roles, work_dir, args.archive_threshold), timelines)
            )
        all_results[config] = [r for res, _ in outs for r in res]
        all_stats[config] = {tl["id"]: st for tl, (_, st) in zip(timelines, outs) if st}
        done = all_results[config]
        print(f"[{config}] {sum(r['correct'] for r in done)}/{len(done)}", file=sys.stderr)

    print(f"模型: {args.model}，检索条数 k={args.k}，写入: {'用户+AI' if args.ingest_assistant else '仅用户'}\n")
    report(all_results)
    if args.out:
        Path(args.out).write_text(
            json.dumps({"results": all_results, "stats": all_stats, "settings": {"ingest_roles": sorted(roles), "decay_archival": False, "retrieval": "bm25", "model": args.model, "k": args.k}}, ensure_ascii=False, indent=1), encoding="utf-8"
        )


if __name__ == "__main__":
    main()
