#!/usr/bin/env python3
"""
语义向量（可选功能）测试

- 用本地假的 OpenAI 兼容 /embeddings 服务测试 openai provider 的完整链路：
  写入后生成向量缓存、未改动的记忆不重复请求、换模型时缓存失效、服务不可用时降级为关键词检索
- MemoryOperator 的语义规则：语义相似只用于找被更正的旧记忆，绝不单独触发合并
"""

import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MEMORY_PY = _REPO_ROOT / "src" / "memory.py"
sys.path.insert(0, str(_REPO_ROOT / "src"))

from embeddings import get_embedder, load_settings  # noqa: E402
from memory_operator import MemoryOperator  # noqa: E402


def fake_vector(text, dim=32):
    """按字符哈希的确定性向量（只用来测链路，不代表语义）"""
    vec = [0.0] * dim
    for ch in text:
        vec[int(hashlib.md5(ch.encode()).hexdigest(), 16) % dim] += 1.0
    return vec


class FakeEmbeddingServer:
    def __init__(self):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(body)
                data = [{"index": i, "embedding": fake_vector(t)} for i, t in enumerate(body["input"])]
                payload = json.dumps({"data": data}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def texts_sent(self):
        return [t for body in self.requests for t in body["input"]]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run(memory_dir, env_extra, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("MEMORY_EMBEDDING_")}
    env.update({"MEMORY_DIR": str(memory_dir), "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1", **env_extra})
    return subprocess.run([sys.executable, str(_MEMORY_PY), *args], env=env, capture_output=True, text=True)


def check(name, ok, detail=""):
    print(f"{'✅ PASS' if ok else '❌ FAIL'}: {name}")
    if detail:
        print(f"       {detail}")
    return int(bool(ok))


def main():
    results = []

    # 1. 默认不启用
    settings = load_settings({})
    results.append(check("默认不启用语义向量", settings["provider"] == "none" and get_embedder(settings) is None))

    server = FakeEmbeddingServer()
    env = {
        "MEMORY_EMBEDDING_PROVIDER": "openai",
        "MEMORY_EMBEDDING_BASE_URL": server.url,
        "MEMORY_EMBEDDING_MODEL": "fake-model",
        "MEMORY_EMBEDDING_API_KEY": "test",
    }

    with tempfile.TemporaryDirectory() as tmp:
        mem = Path(tmp) / "memory"
        run(mem, env, "init")

        # 2. 写入后生成向量缓存
        run(mem, env, "capture", "我对花生过敏")
        run(mem, env, "capture", "我养了一只橘猫叫年糕")
        cache = json.loads((mem / "layer2/index/embeddings.json").read_text(encoding="utf-8"))
        results.append(
            check(
                "capture 后写入向量缓存",
                cache["model"] == "openai:fake-model" and len(cache["vectors"]) == 2,
                f"model={cache['model']} vectors={len(cache['vectors'])}",
            )
        )

        # 3. 每条记忆只计算一次向量，未改动的记忆不重复请求
        sent_before = server.texts_sent().count("我对花生过敏")
        run(mem, env, "rebuild-index")
        sent_after = server.texts_sent().count("我对花生过敏")
        results.append(
            check("每条记忆只计算一次向量", sent_before == 1 and sent_after == 1, f"请求次数 {sent_before} → {sent_after}")
        )

        # 4. 检索时查询会被向量化，且结果是合法 JSON
        proc = run(mem, env, "search", "花生", "--json")
        out = json.loads(proc.stdout)
        results.append(
            check(
                "search --json 使用语义检索",
                "花生" in server.texts_sent() and any("花生" in r["content"] for r in out["results"]),
                f"{out['results']}",
            )
        )

        # 5. 换模型时缓存失效
        run(mem, {**env, "MEMORY_EMBEDDING_MODEL": "other-model"}, "rebuild-index")
        cache = json.loads((mem / "layer2/index/embeddings.json").read_text(encoding="utf-8"))
        results.append(check("换模型后重新计算", cache["model"] == "openai:other-model" and len(cache["vectors"]) == 2))

        # 6. 服务不可用时降级为关键词检索，不报错退出
        down = {**env, "MEMORY_EMBEDDING_BASE_URL": f"http://127.0.0.1:{free_port()}/v1"}
        cap = run(mem, down, "capture", "我不吃香菜")
        proc = run(mem, down, "search", "香菜", "--json")
        ok = cap.returncode == 0 and proc.returncode == 0 and json.loads(proc.stdout)["results"]
        results.append(check("服务不可用时降级为关键词检索", ok, (proc.stderr.strip().splitlines() or [""])[-1]))

    server.httpd.shutdown()

    # 7-8. 语义相似只用于更正，不用于合并
    always_similar = lambda a, b: 0.95  # noqa: E731
    op = MemoryOperator(semantic_similarity=always_similar, semantic_conflict_threshold=0.6)
    old = {"id": "a", "content": "我每天要喝三杯咖啡", "entities": []}
    new = {"id": "b", "content": "我不再喝咖啡了，改喝茶", "entities": []}
    results.append(check("带更正词 + 语义相似 → 识别为更正", op.decide_operation(new, [old])[0] == "DELETE"))

    other = {"id": "c", "content": "我对花粉过敏", "entities": []}
    allergy = {"id": "d", "content": "我对花生过敏", "entities": []}
    results.append(check("无更正词时语义再相似也不合并", op.decide_operation(other, [allergy])[0] == "ADD"))

    passed = sum(results)
    print("\n" + "=" * 60)
    print(f"总计: {passed}/{len(results)} 通过")
    if passed == len(results):
        print("🎉 所有测试通过！")
    print("=" * 60)
    return passed == len(results)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
