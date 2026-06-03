"""
embeddings.py
埋め込み(ベクトル化)バックエンド。
  - sentence-transformers(既定): multilingual-e5 系。query:/passage: プレフィックスを付与。
  - ollama: ollama の埋め込みAPIを使用。
重い依存(torch等)は遅延インポート。
"""
from __future__ import annotations

import threading
from typing import Optional

from .config import settings
from .logging_setup import get_logger

log = get_logger("embeddings")


class Embedder:
    """埋め込みモデルのラッパ。スレッド安全に遅延ロードする。"""

    def __init__(self, backend: Optional[str] = None, model: Optional[str] = None) -> None:
        self.backend = (backend or settings.embed_backend).lower()
        self.model_name = model or settings.embed_model
        self._model = None
        self._lock = threading.Lock()
        # E5系はプレフィックスが必要
        self._use_e5_prefix = "e5" in self.model_name.lower()

    # ---- ロード ----
    def _ensure_st(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    log.info("埋め込みモデル読込中(初回はDLに時間がかかります): %s", self.model_name)
                    from sentence_transformers import SentenceTransformer
                    self._model = SentenceTransformer(self.model_name)
                    log.info("埋め込みモデル準備完了")
        return self._model

    # ---- 内部: プレフィックス ----
    def _doc_text(self, t: str) -> str:
        return f"passage: {t}" if self._use_e5_prefix else t

    def _query_text(self, t: str) -> str:
        return f"query: {t}" if self._use_e5_prefix else t

    # ---- 公開API ----
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self.backend == "ollama":
            return [self._ollama_embed(self._doc_text(t)) for t in texts]
        model = self._ensure_st()
        vecs = model.encode(
            [self._doc_text(t) for t in texts],
            normalize_embeddings=True,
            show_progress_bar=False,
            batch_size=32,
        )
        return [v.tolist() for v in vecs]

    def embed_query(self, text: str) -> list[float]:
        if self.backend == "ollama":
            return self._ollama_embed(self._query_text(text))
        model = self._ensure_st()
        vec = model.encode(
            self._query_text(text),
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return vec.tolist()

    # ---- ollama 埋め込み ----
    def _ollama_embed(self, text: str) -> list[float]:
        import ollama
        client = ollama.Client(host=settings.ollama_host)
        resp = client.embeddings(model=self.model_name, prompt=text)
        return list(resp["embedding"])


# シングルトン(既定設定)
_default_embedder: Optional[Embedder] = None
_default_lock = threading.Lock()


def get_embedder() -> Embedder:
    global _default_embedder
    if _default_embedder is None:
        with _default_lock:
            if _default_embedder is None:
                _default_embedder = Embedder()
    return _default_embedder
