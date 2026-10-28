#!/usr/bin/env python3
"""
LLM 写入判断（可选功能）测试

- parse_decision：各种合法和不合法的输出
- 用本地假的 OpenAI 兼容 /chat/completions 服务跑完整链路：
  SUPERSEDE → 旧记忆失效归档、历史记录 decided_by=llm；DUPLICATE → 不新增；
  没有候选时不调用；服务出错或返回乱码时按规则处理、照常写入
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MEMORY_PY = _REPO_ROOT / "src" / "memory.py"
sys.path.insert(0, str(_REPO_ROOT / "src"))

import llm_judge  # noqa: E402
from llm_judge import parse_decision  # noqa: E402


class FakeChatServer:
    """按新记忆内容返回预设判断；mode 可切换为 error / garbage"""

    def __init__(self):
        self.prompts = []
        self.mode = "normal"
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                prompt = body["messages"][-1]["content"]
                outer.prompts.append(prompt)
                if outer.mode == "error":
                    self.send_response(500)
                    self.end_headers()
                    return
                reply = "我觉得差不多吧" if outer.mode == "garbage" else outer.decide(prompt)
                payload = json.dumps({"choices": [{"message": {"content": reply}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @staticmethod
    def decide(prompt):
        new = re.search(r"新记忆（.*?）：(.*)", prompt).group(1)
        if "分手" in new:
            return '{"decision": "SUPERSEDE", "targets": [1], "reason": "关系已结束"}'
        if "年糕是只橘猫" in new:
            return '```json\n{"decision": "DUPLICATE", "targets": [1], "reason": "同一只猫"}\n```'
        return '{"decision": "ADD", "targets": [], "reason": "新信息"}'


def run(memory_dir, env_extra, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MEMORY_EMBEDDING_", "MEMORY_LLM_"))}
    env.update({"MEMORY_DIR": str(memory_dir), "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1", **env_extra})
    return subprocess.run([sys.executable, str(_MEMORY_PY), *args], env=env, capture_output=True, text=True)


def load(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()] if path.exists() else []


def check(name, ok, detail=""):
    print(f"{'✅ PASS' if ok else '❌ FAIL'}: {name}")
    if detail:
        print(f"       {detail}")
    return int(bool(ok))


def main():
    results = []

    # parse_decision
    cases = [
        ('{"decision": "SUPERSEDE", "targets": [2], "reason": "x"}', 3, "SUPERSEDE"),
        ('好的，结果如下：{"decision": "duplicate", "targets": ["1"]}', 3, "DUPLICATE"),
        ('{"decision": "ADD", "targets": [1]}', 3, "ADD"),
        ('{"decision": "SUPERSEDE", "targets": [4]}', 3, None),  # 编号越界
        ('{"decision": "SUPERSEDE", "targets": []}', 3, None),  # 缺少目标
        ('{"decision": "MAYBE", "targets": [1]}', 3, None),
        ("不是 JSON", 3, None),
    ]
    bad = [(t, got) for t, n, want in cases if ((got := parse_decision(t, n)) or {}).get("decision") != want]
    results.append(check("parse_decision 校验格式和编号", not bad, f"{bad}" if bad else ""))
    results.append(check("ADD 时忽略 targets", parse_decision(cases[2][0], 3)["targets"] == []))

    # 提示词不能包含评测用例的原句片段，否则评测会虚高
    prompt = llm_judge.SYSTEM_PROMPT + llm_judge.USER_PROMPT
    leaks = set()
    for path in (_REPO_ROOT / "eval" / "cases").glob("*.json"):
        for case in json.loads(path.read_text(encoding="utf-8")):
            for session in case["sessions"]:
                for msg in session.get("say", []):
                    text = re.sub(r"[^\w]", "", msg)
                    leaks.update(text[i : i + 4] for i in range(len(text) - 3) if text[i : i + 4] in prompt)
    results.append(check("提示词不含评测用例片段", not leaks, f"{leaks}" if leaks else ""))

    server = FakeChatServer()
    env = {"MEMORY_LLM_JUDGE": "on", "OPENAI_API_KEY": "test", "OPENAI_BASE_URL": server.url}

    with tempfile.TemporaryDirectory() as tmp:
        mem = Path(tmp) / "memory"
        run(mem, env, "init")

        # 没有相关旧记忆时不调用 LLM
        run(mem, env, "capture", "我女朋友叫苏晴")
        results.append(check("没有候选时不调用 LLM", len(server.prompts) == 0, f"调用 {len(server.prompts)} 次"))

        # SUPERSEDE：没有更正词的变化
        run(mem, env, "capture", "我和苏晴分手了")
        active = [r["content"] for r in load(mem / "layer2/active/facts.jsonl")]
        archived = load(mem / "layer2/archive/facts.jsonl")
        history = load(mem / "layer2/history.jsonl")
        ok = (
            "我女朋友叫苏晴" not in active
            and any(r["content"] == "我女朋友叫苏晴" and r.get("superseded_by") for r in archived)
            and any(e["op"] == "INVALIDATE" and e.get("decided_by") == "llm" and e.get("reason") for e in history)
        )
        results.append(check("SUPERSEDE：旧记忆失效归档，历史记录理由", ok, f"活跃: {active}"))

        # DUPLICATE：换个说法的重复（输出带 ```json 代码块也能解析）
        run(mem, env, "capture", "我养了一只橘猫叫年糕")
        run(mem, env, "capture", "我家年糕是只橘猫")
        cats = [r["content"] for r in load(mem / "layer2/active/facts.jsonl") if "年糕" in r["content"]]
        results.append(check("DUPLICATE：不新增重复记忆", len(cats) == 1, f"{cats}"))

        # 提示词里带上候选旧记忆
        results.append(check("提示词包含候选旧记忆", "我养了一只橘猫叫年糕" in server.prompts[-1]))

        # 服务出错 / 返回乱码：按规则处理，照常写入
        server.mode = "error"
        proc = run(mem, env, "capture", "我和苏晴是大学同学")
        server.mode = "garbage"
        proc2 = run(mem, env, "capture", "苏晴现在在杭州工作")
        active = [r["content"] for r in load(mem / "layer2/active/facts.jsonl")]
        ok = proc.returncode == 0 and proc2.returncode == 0 and "我和苏晴是大学同学" in active and "苏晴现在在杭州工作" in active
        results.append(check("LLM 出错或乱码时按规则写入", ok, (proc.stderr.strip().splitlines() or [""])[-1]))

        # 未设置 API Key：不启用，不报错
        proc = run(mem, {"MEMORY_LLM_JUDGE": "on", "OPENAI_BASE_URL": server.url}, "capture", "我喜欢爬山")
        results.append(check("缺少 API Key 时不启用", proc.returncode == 0 and "缺少 OPENAI_API_KEY" in proc.stderr))

    server.httpd.shutdown()

    passed = sum(results)
    print("\n" + "=" * 60)
    print(f"总计: {passed}/{len(results)} 通过")
    if passed == len(results):
        print("🎉 所有测试通过！")
    print("=" * 60)
    return passed == len(results)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
