#!/usr/bin/env python3
"""
从真实对话时间线生成"像朋友一样记得你"的评测题（需要 LLM）

    python3 eval/longterm/make_private_qa.py TIMELINE.json OUT_QA.json [--model deepseek-flash]

三步：
1. 分段抽取：每 14 天一段，抽出关于用户本人的可记忆事实（带日期和原话依据）
2. 组题：按类别出题（单点事实 / 跨天整合 / 时间 / 状态更新 / 偏好 / 没说过）
3. 核验：把完整记录交给 LLM，核对每题答案有依据、"没说过"的题确实没说过；不合格的丢弃

输出 QA 不含原始对话；回放用的时间线由 extract_sessions.py 从归档重新生成。
"""

import argparse
import json
import re
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_longterm import LLM  # noqa: E402

CATEGORIES = {
    "extraction": ("单点事实：只在某一天提到过的具体信息", 16),
    "multi_session": ("跨天整合：答案需要结合不同日期说过的两件以上的事", 8),
    "temporal": ("时间：什么时候做/说了某事、两件事谁先谁后、隔了多久（日期要具体）", 8),
    "update": ("状态更新：前后说法变了（计划、工具、决定、项目状态），问现在/最后的状态", 8),
    "preference": ("偏好：根据用户表达过的喜好或习惯，问一个需要个性化回答的问题", 6),
    "abstention": ("没说过：用户从未提过、但听起来合理的个人问题（正确回答是不记得/没提过）", 8),
}

EXTRACT_SYSTEM = "你在帮一个 AI 伙伴整理它应该记住的、关于用户本人的事。只输出 JSON。"
EXTRACT_PROMPT = """下面是用户在 {start} 到 {end} 之间对 AI 说的话（只有用户的话，按时间排列）。

{text}

列出值得长期记住的、关于用户本人的事实：身份与背景、在做的项目和业务、做过的决定、
计划与安排、偏好与习惯、遇到的问题、提到的人（用角色称呼，不写真名）、使用的工具等。
不要记代码细节、报错内容、一次性的操作指令。不要包含密码、密钥、手机号、邮箱、身份证号。

只输出 JSON：{{"facts": [{{"date": "YYYY-MM-DD", "fact": "一句完整的话，以“用户”开头", "quote": "原话里的关键片段（20字以内）"}}]}}"""

COMPOSE_SYSTEM = "你在为一个 AI 记忆系统出评测题，检验它能不能像老朋友一样记得用户说过的话。只输出 JSON。"
COMPOSE_PROMPT = """下面是用户在 {start} 到 {end} 期间说过的、值得记住的事（按日期）：

{facts}

今天是 {today}。请出 {n} 道"{name}"类题：{desc}。

要求：
- 用用户自己的口吻向 AI 提问，像日常聊天（如"你还记得我之前说……吗""我上次……是什么时候"）
- 每题答案必须能从上面的事实得出（"没说过"类除外），答案简短具体
- 不要包含密码、密钥、联系方式、他人真名
- 不要出重复或几乎相同的题

只输出 JSON：{{"questions": [{{"q": "问题", "answer": "标准答案（没说过类写 null）", "evidence_dates": ["YYYY-MM-DD"]}}]}}"""

VERIFY_SYSTEM = "你是严格的审题员，根据完整对话记录检查评测题。只输出 JSON。"
VERIFY_PROMPT = """{transcript}

今天是 {today}。待检查的题：
类别：{category}
问题：{q}
标准答案：{answer}

检查：
- 若类别是"没说过"：记录里确实没有能回答这个问题的信息，才算合格。
- 其他类别：标准答案被记录明确支持、没有歧义、和问题对得上，才算合格；若答案需要小修正，给出修正后的答案。

只输出 JSON：{{"valid": true 或 false, "fixed_answer": "修正后的答案或 null", "reason": "一句话"}}"""


def _json(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def chunks(timeline, days=14):
    sessions = timeline["sessions"]
    start = date.fromisoformat(sessions[0]["date"])
    group = []
    for s in sessions:
        if group and date.fromisoformat(s["date"]) - start >= timedelta(days=days):
            yield group
            group, start = [], date.fromisoformat(s["date"])
        group.append(s)
    if group:
        yield group


def render(sessions, max_chars=1500):
    lines = []
    for s in sessions:
        lines.append(f"## {s['date']}")
        lines += [f"- {t['text'][:max_chars]}" for t in s["turns"]]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("timeline")
    ap.add_argument("out")
    ap.add_argument("--model", default="deepseek-flash")
    ap.add_argument("--cache", default=str(Path(tempfile.gettempdir()) / "longterm-llm-cache"))
    args = ap.parse_args()

    llm = LLM(args.model, Path(args.cache))
    tl = json.loads(Path(args.timeline).read_text(encoding="utf-8"))[0]
    # 出题和核验只看用户的话
    tl = {**tl, "sessions": [{**s, "turns": [t for t in s["turns"] if t["role"] == "user"]} for s in tl["sessions"]]}
    tl["sessions"] = [s for s in tl["sessions"] if s["turns"]]
    first, last = tl["sessions"][0]["date"], tl["sessions"][-1]["date"]
    today = (date.fromisoformat(last) + timedelta(days=1)).isoformat()

    facts = []
    for group in chunks(tl):
        data = _json(llm(EXTRACT_SYSTEM, EXTRACT_PROMPT.format(start=group[0]["date"], end=group[-1]["date"], text=render(group)), max_tokens=8192))
        facts += (data or {}).get("facts", [])
    facts.sort(key=lambda f: f.get("date", ""))
    print(f"抽取事实 {len(facts)} 条", file=sys.stderr)
    fact_text = "\n".join(f"- （{f.get('date')}）{f.get('fact')}" for f in facts)

    candidates = []
    for cat, (desc, n) in CATEGORIES.items():
        name = desc.split("：")[0]
        data = _json(llm(COMPOSE_SYSTEM, COMPOSE_PROMPT.format(start=first, end=last, facts=fact_text, today=today, n=n, name=name, desc=desc), max_tokens=8192))
        for q in (data or {}).get("questions", []):
            candidates.append({"q": q.get("q", ""), "answer": q.get("answer"), "category": cat, "evidence": ",".join(q.get("evidence_dates") or [])})
    print(f"候选题 {len(candidates)} 道", file=sys.stderr)

    transcript = "【完整对话记录（只有用户的话）】\n" + render(tl["sessions"], max_chars=4000)
    kept = []
    for c in candidates:
        if not c["q"] or (c["category"] != "abstention" and not c["answer"]):
            continue
        v = _json(llm(VERIFY_SYSTEM, VERIFY_PROMPT.format(transcript=transcript, today=today, category=CATEGORIES[c["category"]][0].split("：")[0], q=c["q"], answer=c["answer"]), max_tokens=1024))
        if not v or not v.get("valid"):
            c["rejected"] = (v or {}).get("reason", "无法解析")
            continue
        if c["category"] != "abstention" and v.get("fixed_answer"):
            c["answer"] = v["fixed_answer"]
        if c["category"] == "abstention":
            c["answer"] = None
        kept.append(c)

    by_cat = {}
    for c in kept:
        by_cat[c["category"]] = by_cat.get(c["category"], 0) + 1
    print(f"核验通过 {len(kept)}/{len(candidates)} 道 {by_cat}", file=sys.stderr)
    Path(args.out).write_text(
        json.dumps({"today": today, "facts_extracted": len(facts), "questions": kept}, ensure_ascii=False, indent=1), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
