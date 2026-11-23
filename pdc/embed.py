"""向量化：把 chunk 变成稠密向量。

设计要点：
  1. 抽象成 Protocol —— 换模型（bge / OpenAI / 内部网关）只改一个类
  2. 磁盘缓存 —— 全量重算要几秒，缓存后 0.1 秒（语料越大差距越明显）
  3. 启动时检查环境 —— HF 不通时给出明确提示，而不是 30 秒超时后抛栈

关于网络：默认走 HuggingFace；在国内或内网环境设 HF_ENDPOINT=https://hf-mirror.com。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Protocol

import numpy as np

DEFAULT_MODEL = os.getenv("PDC_EMBED_MODEL", "BAAI/bge-small-zh-v1.5")
CACHE_DIR = Path(__file__).resolve().parents[1] / "out" / "rag"


class Embedder(Protocol):
    name: str

    @property
    def dim(self) -> int: ...

    def encode(self, texts: list[str]) -> np.ndarray: ...


class FastEmbedder:
    """fastembed 本地 ONNX 推理，不需要 GPU，不需要 API key。"""

    def __init__(self, model_name: str = DEFAULT_MODEL, cache_dir: Path | None = None) -> None:
        self.name = model_name
        self.cache_dir = Path(cache_dir or CACHE_DIR)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._model: Any = None
        self._dim = 0

    def _load(self):
        if self._model is None:
            from fastembed import TextEmbedding

            try:
                self._model = TextEmbedding(model_name=self.name)
            except Exception as exc:
                raise RuntimeError(
                    f"无法加载嵌入模型 {self.name}。\n"
                    f"国内/内网环境请先设置: export HF_ENDPOINT=https://hf-mirror.com\n"
                    f"原始错误: {exc}"
                ) from exc
        return self._model

    def _cache_path(self, texts: list[str]) -> Path:
        key = hashlib.sha1(("\n".join(texts) + self.name).encode()).hexdigest()[:16]
        return self.cache_dir / f"emb-{self.name.replace('/', '_')}-{key}.npy"

    def encode(self, texts: list[str]) -> np.ndarray:
        # 只有批量编码（语料）才落缓存；单条 query 每次都不一样，
        # 缓存它只会把目录塞满小文件。
        use_cache = len(texts) > 8
        cached = self._cache_path(texts) if use_cache else None
        if cached is not None and cached.exists():
            arr = np.load(cached)
            self._dim = arr.shape[1]
            return arr

        model = self._load()
        arr = np.array(list(model.embed(texts)), dtype=np.float32)
        # 归一化，后面直接点积即余弦相似度
        norm = np.linalg.norm(arr, axis=1, keepdims=True)
        arr = arr / np.clip(norm, 1e-12, None)
        if cached is not None:
            np.save(cached, arr)
        self._dim = arr.shape[1]
        return arr

    @property
    def dim(self) -> int:
        return self._dim


class HashEmbedder:
    """只在测试和离线演示里用的确定性占位实现。

    它不是语义模型，只保证：
      - 接口一致
      - 相同输入得到相同向量
      - 不同词得到不同方向
    真实评测必须用 FastEmbedder，否则结论没有意义。
    """

    name = "hash-baseline"

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    def encode(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            for token in text.lower().split():
                h = int(hashlib.md5(token.encode()).hexdigest()[:8], 16) % self.dim
                out[i, h] += 1.0
        norm = np.linalg.norm(out, axis=1, keepdims=True)
        return out / np.clip(norm, 1e-12, None)


def get_embedder(offline: bool = False) -> Embedder:
    if offline or os.getenv("PDC_OFFLINE_EMBED") == "1":
        return HashEmbedder()
    return FastEmbedder()


def cosine(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """a: (d,) 或 (n,d)，b: (m,d) -> (m,) 或 (n,m)。输入已归一化时即点积。"""
    return a @ b.T


def write_manifest(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
