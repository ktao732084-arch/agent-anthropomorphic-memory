#!/usr/bin/env python3
"""
"像人一样记住"相关行为（通过 CLI，临时记忆目录）

- AI 说过的话也记住，并标明是 AI 说的
- AI 的话不能更正或覆盖用户自己的记忆
- 衰减只影响排序：很久以前的记忆淡忘（dormant），但仍能检索到
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_MEMORY_PY = Path(__file__).resolve().parent.parent / "src" / "memory.py"


def run(memory_dir, now, *args):
    env = {
        **os.environ,
        "MEMORY_DIR": str(memory_dir),
        "MEMORY_NOW": now,
        "MEMORY_EMBEDDING_PROVIDER": "none",
        "MEMORY_LLM_JUDGE": "off",
    }
    return subprocess.run([sys.executable, str(_MEMORY_PY), *args], env=env, capture_output=True, text=True, check=True).stdout


def active(memory_dir):
    out = []
    for name in ("facts", "beliefs", "summaries"):
        p = memory_dir / f"layer2/active/{name}.jsonl"
        if p.exists():
            out += [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    return out


def check(name, ok, detail=""):
    print(f"{'✅ PASS' if ok else '❌ FAIL'}: {name}")
    if detail:
        print(f"       {detail}")
    return int(bool(ok))


def main():
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        mem = Path(tmp) / "memory"
        run(mem, "2026-03-01T08:00:00Z", "init")
        run(mem, "2026-03-01T10:00:00Z", "add-pending", "记住，我对花生过敏，很严重")
        run(mem, "2026-03-01T23:00:00Z", "consolidate", "--force")

        run(mem, "2026-03-02T09:00:00Z", "add-pending", "[[reply_to_current]] 好嘞！", "--source", "assistant")
        run(mem, "2026-03-02T09:30:00Z", "add-pending", "你还记得我上次说的那个项目吗？")
        run(mem, "2026-03-02T10:00:00Z", "add-pending", "[[reply_to_current]] 我推荐你看《百年孤独》，很适合你现在的状态", "--source", "assistant")
        run(mem, "2026-03-02T10:01:00Z", "add-pending", "其实是对花粉过敏的人更多一些，你可以去医院查一下过敏原", "--source", "assistant")
        run(mem, "2026-03-02T23:00:00Z", "consolidate", "--force")
        records = active(mem)
        contents = [r["content"] for r in records]

        results.append(
            check(
                "AI 的推荐被记住，并标明是 AI 说的（回复指令已去掉）",
                any(c.startswith("（AI 说）我推荐") and "百年孤独" in c for c in contents),
                f"{contents}",
            )
        )
        results.append(
            check(
                "AI 的短应答和用户的回忆型提问不记",
                not any("好嘞" in c or "你还记得" in c for c in contents),
                f"{contents}",
            )
        )
        peanut = next((r for r in records if "花生" in r["content"] and r.get("source") != "assistant"), None)
        results.append(
            check(
                "AI 说\"其实是……\"不会让用户自己的记忆失效",
                peanut is not None and not peanut.get("invalid_at") and not peanut.get("conflict_downgraded"),
                f"{peanut}",
            )
        )

        # 三年后：分数衰减到阈值以下，但仍在活跃池、仍能检索
        run(mem, "2029-03-02T23:00:00Z", "consolidate", "--force")
        records = active(mem)
        found = json.loads(run(mem, "2029-03-03T09:00:00Z", "search", "百年孤独", "--json"))["results"]
        results.append(
            check(
                "很久以前的记忆淡忘但不消失",
                any(r.get("dormant") for r in records) and any("百年孤独" in r["content"] for r in found),
                f"dormant={[r.get('dormant') for r in records]} 检索={[r['content'] for r in found]}",
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
