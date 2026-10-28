#!/usr/bin/env python3
"""
collect_from_sessions.py 测试（合成数据）

真实 OpenClaw 渠道消息外面包着元数据，旧版收集器直接跳过含
"Conversation info (untrusted metadata)" 的消息，其余的又把外壳原样写进记忆。
"""

import json
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from collect_from_sessions import extract_user_messages, user_text  # noqa: E402

ENVELOPE = (
    'Conversation info (untrusted metadata):\n```json\n{\n  "chat_id": "123",\n  "channel": "telegram"\n}\n```\n\n'
    'Sender (untrusted metadata):\n```json\n{"name": "someone"}\n```\n\n'
    "[Sun 2026-03-01 10:00 GMT+8] "
)


def entry(role, text, ts="2026-03-01T02:00:00Z"):
    return {"type": "message", "timestamp": ts, "message": {"role": role, "content": [{"type": "text", "text": text}]}}


def check(name, ok, detail=""):
    print(f"{'✅ PASS' if ok else '❌ FAIL'}: {name}")
    if detail:
        print(f"       {detail}")
    return int(bool(ok))


def main():
    results = []
    results.append(check("剥掉元数据外壳和时间戳", user_text(ENVELOPE + "我下周三有面试") == "我下周三有面试"))
    results.append(
        check(
            "系统事件、定时任务、心跳、压缩提示不算用户的话",
            all(
                user_text(t) is None
                for t in [
                    "System: [2026-03-01 10:00:00 GMT+8] Exec completed",
                    "[cron:abc123] 每日简报",
                    "Read HEARTBEAT.md if it exists",
                    "Pre-compaction memory flush. Save durable memories now.",
                ]
            ),
        )
    )

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "s.jsonl"
        lines = [
            json.dumps(entry("user", ENVELOPE + "记住，我对青霉素过敏"), ensure_ascii=False),
            json.dumps(entry("assistant", "好的，记住了"), ensure_ascii=False),
            json.dumps(entry("user", "System: [2026-03-01] heartbeat"), ensure_ascii=False),
            json.dumps({"type": "session", "id": "x"}),
            # 脱敏工具留下的非法转义（反斜杠 + x），json.loads 会直接报错
            '{"type": "message", "timestamp": "2026-03-01T03:00:00Z", "message": {"role": "user", '
            '"content": [{"type": "text", "text": "我的项目放在 C:\\\\Users\\x\\\\code 下面"}]}}',
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        msgs = [m["content"] for m in extract_user_messages(path, hours=None)]
        results.append(
            check(
                "只收集用户原话，修复非法转义的行",
                msgs == ["记住，我对青霉素过敏", "我的项目放在 C:\\Users\\x\\code 下面"],
                f"{msgs}",
            )
        )

    passed = sum(results)
    print("\n" + "=" * 60)
    print(f"总计: {passed}/{len(results)} 通过")
    if passed == len(results):
        print("🎉 所有测试通过！")
    print("=" * 60)
    return passed == len(results)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
