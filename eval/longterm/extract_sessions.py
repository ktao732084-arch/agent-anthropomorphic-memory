#!/usr/bin/env python3
"""
从 session-archive（OpenClaw / Claude Code / Codex 会话归档）提取用户本人说过的话，
合并成一条按时间排序的统一格式时间线（questions 为空，由 make_private_qa.py 生成）。

    python3 eval/longterm/extract_sessions.py ARCHIVE_DIR OUT.json [--openclaw-dirs tkao-vps/sessions,...]

只收用户原话：去掉渠道元数据外壳、系统事件、命令输出、系统提醒、工具结果；
超过 MAX_CHARS 的大段粘贴（日志、文档）不算对话，跳过。
"""

import argparse
import glob
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from collect_from_sessions import load_jsonl_lenient, user_text  # noqa: E402

MAX_CHARS = 4000
_CC_SKIP = ("<command-", "<local-command", "Caveat:", "[Request interrupted", "This session is being continued")
_SYSTEM_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_CODEX_SKIP = ("<environment_context>", "<user_instructions>", "# AGENTS.md", "<turn_aborted>")


def _records(path):
    for line in open(path, encoding="utf-8", errors="ignore"):
        if line.strip():
            try:
                yield load_jsonl_lenient(line)
            except ValueError:
                continue


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") in ("text", "input_text"))
    return ""


def openclaw_messages(path):
    """OpenClaw 会话里用户和 AI 的话（与插件收集器一致：AI 回复也收）"""
    for r in _records(path):
        m = r.get("message")
        if r.get("type") != "message" or not isinstance(m, dict):
            continue
        if m.get("role") == "user":
            text = user_text(_text(m.get("content")))
            if text:
                yield r.get("timestamp", ""), text, "openclaw", "user"
        elif m.get("role") == "assistant":
            text = _text(m.get("content")).strip()
            if text:
                yield r.get("timestamp", ""), text, "openclaw", "assistant"


def claude_code_messages(path):
    for r in _records(path):
        if r.get("type") != "user" or r.get("isMeta"):
            continue
        content = (r.get("message") or {}).get("content")
        if isinstance(content, list) and any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
            continue
        text = _SYSTEM_REMINDER.sub("", _text(content)).strip()
        if text and not text.startswith(_CC_SKIP):
            yield r.get("timestamp", ""), text, "claude-code", "user"


def codex_messages(path):
    for r in _records(path):
        p = r.get("payload")
        if r.get("type") == "event_msg" and isinstance(p, dict) and p.get("type") == "user_message":
            text = (p.get("message") or "").strip()
            if text and not text.startswith(_CODEX_SKIP):
                yield r.get("timestamp", ""), text, "codex", "user"


def build_timeline(archive: Path, openclaw_dirs):
    messages = []
    for d in openclaw_dirs:
        for f in glob.glob(str(archive / "openclaw-sessions" / d / "*.jsonl")):
            messages += openclaw_messages(f)
    for f in glob.glob(str(archive / "claude-code-sessions/**/*.jsonl"), recursive=True):
        if "/subagents/" not in f:
            messages += claude_code_messages(f)
    for f in glob.glob(str(archive / "codex-sessions/**/*.jsonl"), recursive=True):
        messages += codex_messages(f)

    seen = set()
    by_day = defaultdict(list)
    skipped_long = 0
    for ts, text, src, role in sorted(messages):
        if not ts[:10] or (text, ts[:10]) in seen:
            continue
        seen.add((text, ts[:10]))
        if len(text) > MAX_CHARS:
            skipped_long += 1
            continue
        by_day[ts[:10]].append({"role": role, "speaker": role, "text": text, "source": src, "time": ts})
    sessions = [{"date": day, "turns": turns} for day, turns in sorted(by_day.items())]
    return {"id": "private-user", "source": "private", "user": "user", "sessions": sessions, "questions": []}, skipped_long


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("archive")
    ap.add_argument("out")
    ap.add_argument("--openclaw-dirs", default="tkao-vps/sessions,tkao-vps/laptop-sessions")
    args = ap.parse_args()
    tl, skipped = build_timeline(Path(args.archive), args.openclaw_dirs.split(","))
    Path(args.out).write_text(json.dumps([tl], ensure_ascii=False, indent=1), encoding="utf-8")
    turns = [t for s in tl["sessions"] for t in s["turns"]]
    by_src = defaultdict(int)
    for t in turns:
        by_src[f"{t['source']}/{t['role']}"] += 1
    print(
        f"{len(tl['sessions'])} 天（{tl['sessions'][0]['date']} ~ {tl['sessions'][-1]['date']}），"
        f"{len(turns)} 条消息 {dict(by_src)}，共 {sum(len(t['text']) for t in turns)} 字；跳过超长粘贴 {skipped} 条"
    )


if __name__ == "__main__":
    main()
