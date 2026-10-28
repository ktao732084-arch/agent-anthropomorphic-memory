#!/usr/bin/env python3
"""
可选的语义向量（embedding）支持

默认关闭，不启用时系统仍是纯标准库、纯规则。启用后用于：
1. 语义检索：查询和记忆用词不同也能找到（宠物 → 橘猫、家乡 → 老家）
2. 更正匹配：带更正词、但和旧记忆字面重叠很少时（"不再喝咖啡" → "每天三杯咖啡"），
   用语义相似度找到被更正的旧记忆

注意：embedding 不能区分"同一件事换个说法"和"换了一个词的不同事实"
（"花生过敏 / 花粉过敏"相似度和真正的改写一样高），所以这里不用它决定合并。

两种来源，都通过环境变量（优先）或 config.json 的 "embedding" 段配置：

    MEMORY_EMBEDDING_PROVIDER      none（默认）| openai | onnx
    MEMORY_EMBEDDING_QUERY_PREFIX  查询前缀，bge 中文模型建议设为
                                   "为这个句子生成表示以用于检索相关文章："

    # openai：任何 OpenAI 兼容的 /embeddings 接口（OpenAI、硅基流动、Ollama、LM Studio 等），只用标准库
    MEMORY_EMBEDDING_BASE_URL      默认取 OPENAI_BASE_URL，再默认 https://api.openai.com/v1
    MEMORY_EMBEDDING_MODEL         默认 text-embedding-3-small
    MEMORY_EMBEDDING_API_KEY       默认取 OPENAI_API_KEY

    # onnx：本地模型目录（含 model.onnx 和 tokenizer.json），需要 pip install onnxruntime tokenizers numpy
    MEMORY_EMBEDDING_MODEL_DIR     例如 bge-small-zh-v1.5 的 ONNX 导出
"""

import hashlib
import json
import math
import os
import sys
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

DEFAULTS = {
    "provider": "none",
    "query_prefix": "",
    "base_url": None,
    "model": "text-embedding-3-small",
    "api_key": None,
    "model_dir": None,
    # 语义检索结果的最低余弦相似度
    "search_min_score": 0.33,
    # 带更正词时，语义相似度达到此值视为在更正这条旧记忆
    "correction_min_score": 0.6,
}

_ENV = {
    "provider": "MEMORY_EMBEDDING_PROVIDER",
    "query_prefix": "MEMORY_EMBEDDING_QUERY_PREFIX",
    "base_url": "MEMORY_EMBEDDING_BASE_URL",
    "model": "MEMORY_EMBEDDING_MODEL",
    "api_key": "MEMORY_EMBEDDING_API_KEY",
    "model_dir": "MEMORY_EMBEDDING_MODEL_DIR",
}


def load_settings(config: Optional[Dict] = None) -> Dict:
    """合并默认值、config.json 的 embedding 段和环境变量（环境变量优先）"""
    settings = dict(DEFAULTS)
    settings.update((config or {}).get("embedding", {}))
    for key, env in _ENV.items():
        if os.environ.get(env):
            settings[key] = os.environ[env]
    settings["base_url"] = settings["base_url"] or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    settings["api_key"] = settings["api_key"] or os.environ.get("OPENAI_API_KEY")
    return settings


def _normalize(vec: List[float]) -> List[float]:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def cosine(a: List[float], b: List[float]) -> float:
    """两个已归一化向量的余弦相似度"""
    return sum(x * y for x, y in zip(a, b))


class OpenAIEmbedder:
    """OpenAI 兼容的 /embeddings 接口，只用标准库"""

    def __init__(self, settings: Dict):
        self.url = settings["base_url"].rstrip("/") + "/embeddings"
        self.model = settings["model"]
        self.api_key = settings["api_key"]
        self.model_id = f"openai:{self.model}"

    def embed(self, texts: List[str]) -> List[List[float]]:
        body = json.dumps({"model": self.model, "input": texts}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(self.url, data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))["data"]
        data.sort(key=lambda d: d["index"])
        return [_normalize(d["embedding"]) for d in data]


class OnnxEmbedder:
    """本地 ONNX 模型（如 bge-small-zh-v1.5），需要 onnxruntime、tokenizers、numpy"""

    def __init__(self, settings: Dict):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        model_dir = Path(settings["model_dir"])
        self.np = np
        self.tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.tokenizer.enable_truncation(512)
        self.tokenizer.enable_padding()
        self.session = ort.InferenceSession(str(model_dir / "model.onnx"), providers=["CPUExecutionProvider"])
        self.input_names = {i.name for i in self.session.get_inputs()}
        self.output_names = [o.name for o in self.session.get_outputs()]
        self.model_id = f"onnx:{model_dir.name}"

    def embed(self, texts: List[str]) -> List[List[float]]:
        np = self.np
        encoded = self.tokenizer.encode_batch(texts)
        feeds = {
            "input_ids": np.array([e.ids for e in encoded], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in encoded], dtype=np.int64),
            "token_type_ids": np.array([e.type_ids for e in encoded], dtype=np.int64),
        }
        feeds = {k: v for k, v in feeds.items() if k in self.input_names}
        if "sentence_embedding" in self.output_names:
            vecs = self.session.run(["sentence_embedding"], feeds)[0]
        else:
            # 只有 last_hidden_state 时取 [CLS]（bge 系列的官方池化方式）
            vecs = self.session.run([self.output_names[0]], feeds)[0][:, 0, :]
        vecs = vecs / np.linalg.norm(vecs, axis=1, keepdims=True)
        return vecs.tolist()


_EMBEDDER_CACHE: Dict[str, object] = {}


def get_embedder(settings: Dict):
    """按配置返回 embedder；未启用或依赖缺失时返回 None（并在 stderr 说明原因）"""
    provider = (settings.get("provider") or "none").lower()
    if provider == "none":
        return None
    key = json.dumps({k: settings.get(k) for k in ("provider", "model", "base_url", "model_dir")}, sort_keys=True)
    if key in _EMBEDDER_CACHE:
        return _EMBEDDER_CACHE[key]
    try:
        if provider == "openai":
            embedder = OpenAIEmbedder(settings)
        elif provider == "onnx":
            if not settings.get("model_dir"):
                raise ValueError("未设置 MEMORY_EMBEDDING_MODEL_DIR")
            embedder = OnnxEmbedder(settings)
        else:
            raise ValueError(f"未知的 embedding provider: {provider}")
    except Exception as e:
        print(f"⚠️ 语义向量未启用: {e}", file=sys.stderr)
        embedder = None
    _EMBEDDER_CACHE[key] = embedder
    return embedder


def content_key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


class VectorStore:
    """
    记忆向量缓存：layer2/index/embeddings.json

    按内容哈希存储，内容不变就不重算；换模型时整体失效。
    """

    def __init__(self, memory_dir: Path, embedder, settings: Dict):
        self.path = memory_dir / "layer2/index/embeddings.json"
        self.embedder = embedder
        self.settings = settings
        self.query_prefix = settings.get("query_prefix") or ""
        self.vectors: Dict[str, List[float]] = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if data.get("model") == embedder.model_id:
                    self.vectors = data.get("vectors", {})
            except (ValueError, OSError):
                pass

    def ensure(self, texts: List[str]) -> int:
        """为缺少向量的文本计算向量，返回新计算的条数"""
        missing = sorted({t for t in texts if content_key(t) not in self.vectors})
        for i in range(0, len(missing), 32):
            batch = missing[i : i + 32]
            for text, vec in zip(batch, self.embedder.embed(batch)):
                self.vectors[content_key(text)] = [round(x, 5) for x in vec]
        return len(missing)

    def prune(self, texts: List[str]):
        """只保留给定文本的向量（记忆被删除或改写后清理）"""
        keep = {content_key(t) for t in texts}
        self.vectors = {k: v for k, v in self.vectors.items() if k in keep}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"model": self.embedder.model_id, "vectors": self.vectors}
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def get(self, text: str) -> Optional[List[float]]:
        return self.vectors.get(content_key(text))

    def embed_query(self, query: str) -> List[float]:
        return self.embedder.embed([self.query_prefix + query])[0]

    def similarity(self, text1: str, text2: str) -> float:
        """两条记忆之间的语义相似度（不加查询前缀）"""
        self.ensure([text1, text2])
        return cosine(self.get(text1), self.get(text2))
