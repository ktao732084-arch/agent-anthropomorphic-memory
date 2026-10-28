#!/usr/bin/env python3
"""
回归测试：被判为 summary 类型的消息不能让 consolidate 崩溃

旧代码用 f"{mem_type}s" 拼字典键，summary 拼出 "summarys"（应为 summaries），
"总的来说……""总结一下……"这类日常说法会让整次 consolidate 抛 KeyError，
而 Phase 1 已经清空了 pending，当天的消息全部丢失。
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_MEMORY_PY = Path(__file__).resolve().parent.parent / "src" / "memory.py"


def run(memory_dir, *args):
    env = {**os.environ, "MEMORY_DIR": str(memory_dir), "MEMORY_EMBEDDING_PROVIDER": "none", "MEMORY_LLM_JUDGE": "off"}
    return subprocess.run([sys.executable, str(_MEMORY_PY), *args], env=env, capture_output=True, text=True)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        mem = Path(tmp) / "memory"
        run(mem, "init")
        for text in ["总的来说我这周过得不错，项目进展顺利", "我对青霉素过敏"]:
            run(mem, "add-pending", text)
        proc = run(mem, "consolidate", "--force")
        stored = [
            json.loads(line)["content"]
            for name in ("facts", "beliefs", "summaries")
            for line in (mem / f"layer2/active/{name}.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        ok = proc.returncode == 0 and "Traceback" not in proc.stderr and any("青霉素" in s for s in stored)
        print(f"{'✅ PASS' if ok else '❌ FAIL'}: summary 类型不导致 consolidate 崩溃")
        if not ok:
            print(proc.stderr[-800:])
        print("\n" + "=" * 60)
        print(f"总计: {int(ok)}/1 通过")
        if ok:
            print("🎉 所有测试通过！")
        print("=" * 60)
        return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
