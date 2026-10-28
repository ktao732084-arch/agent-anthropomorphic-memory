#!/usr/bin/env python3
"""
Agent 化相关功能的端到端测试（通过 CLI 调用，使用临时记忆目录）

- 相对时间落地 + 未来事件过去后仍可检索
- 强修正 → 旧记忆写入 invalid_at 并移入归档池（不删除）
- 操作历史 history.jsonl
- search --json 只输出可解析的 JSON；get 按 ID 取全文（含归档池）
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MEMORY_PY = _REPO_ROOT / "src" / "memory.py"


def run(memory_dir, *args):
    env = {**os.environ, "MEMORY_DIR": str(memory_dir)}
    out = subprocess.run(
        [sys.executable, str(_MEMORY_PY), *args], env=env, capture_output=True, text=True, check=True
    )
    return out.stdout


def load(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def check(name, ok, detail=""):
    print(f"{'✅ PASS' if ok else '❌ FAIL'}: {name}")
    if detail:
        print(f"       {detail}")
    return int(bool(ok))


def main():
    passed = total = 0
    with tempfile.TemporaryDirectory() as tmp:
        mem = Path(tmp) / "memory"
        run(mem, "init")

        # 1. 相对时间落地
        run(mem, "capture", "我下周三有个重要考试")
        facts = load(mem / "layer2/active/facts.jsonl")
        exam = facts[0]
        total += 1
        passed += check(
            "相对时间换算为绝对日期",
            "（20" in exam["content"] and exam.get("event_date") and exam.get("expires_at"),
            f"{exam['content']} event_date={exam.get('event_date')} expires_at={exam.get('expires_at')}",
        )

        # 2. 强修正 → 失效并归档
        run(mem, "capture", "记住，我对花生过敏，很严重")
        run(mem, "capture", "更正：我对花生不过敏了")
        active = load(mem / "layer2/active/facts.jsonl")
        archived = load(mem / "layer2/archive/facts.jsonl")
        old = next((r for r in archived if "很严重" in r["content"]), None)
        total += 1
        passed += check(
            "强修正后旧记忆移入归档并记录 superseded_by",
            old is not None
            and old.get("invalid_at")
            and old.get("superseded_by")
            and not any("很严重" in r["content"] for r in active),
            f"归档: {[r['content'] for r in archived]}",
        )

        # 3. 历史记录
        ops = [e["op"] for e in load(mem / "layer2/history.jsonl")]
        total += 1
        passed += check("history.jsonl 记录 ADD/INVALIDATE/ARCHIVE", {"ADD", "INVALIDATE", "ARCHIVE"} <= set(ops), f"{ops}")

        # 4. search --json 可直接解析
        result = json.loads(run(mem, "search", "花生", "--json"))
        ids = [r["id"] for r in result["results"]]
        total += 1
        passed += check(
            "search --json 只输出 JSON 且不返回已失效记忆",
            ids and all("很严重" not in r["content"] for r in result["results"]),
            f"{result}",
        )

        # 5. get 能取到归档池里的旧记忆
        got = json.loads(run(mem, "get", old["id"] if old else "missing", *ids[:1]))
        total += 1
        passed += check(
            "get 按 ID 取全文（含归档池）",
            not got["missing"] and {m["pool"] for m in got["memories"]} == {"active", "archive"},
            f"pools={[m['pool'] for m in got['memories']]} missing={got['missing']}",
        )

        # 6. 过期事件在下一次 consolidate 时标记，仍保留
        rows = load(mem / "layer2/active/facts.jsonl")
        for r in rows:
            if r.get("expires_at"):
                r["expires_at"] = "2000-01-01T00:00:00Z"
        (mem / "layer2/active/facts.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
        )
        run(mem, "consolidate", "--force")
        active = load(mem / "layer2/active/facts.jsonl")
        total += 1
        passed += check(
            "事件过去后仍记得，只标记 event_passed",
            any(r.get("event_passed") and "考试" in r["content"] for r in active),
            f"{[(r.get('event_passed'), r['content']) for r in active]}",
        )

    print("\n" + "=" * 60)
    print(f"总计: {passed}/{total} 通过")
    if passed == total:
        print("🎉 所有测试通过！")
    print("=" * 60)
    return passed == total


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
