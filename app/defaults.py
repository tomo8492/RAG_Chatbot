"""
defaults.py
生成・RAG パラメータの既定値と、会話ごとの実効値マージ。
  base(config由来) <- グローバル既定(kv "defaults") <- 会話固有(conversation)
"""
from __future__ import annotations

from . import db
from .config import settings
from .llm import DEFAULT_SYSTEM_PROMPT

# UIに出す選択肢の定義(参考)
EFFORT_CHOICES = ["off", "low", "medium", "high", "max"]
LENGTH_PRESETS = {"short": 512, "standard": 1024, "long": 2048, "max": 4096}


def base_defaults() -> dict:
    return {
        "model": settings.chat_model,
        # 画像(スクショ)付き質問のときに使う Vision/OCR モデル。
        # 設定画面で GLM-OCR など任意の対応モデルに切り替えられる。
        "vision_model": settings.vision_model,
        "temperature": 0.3,
        "top_p": 0.9,
        "num_predict": 1024,      # 回答の最大トークン(長さ)
        "num_ctx": 0,             # 0 = モデル既定のコンテキスト長
        "effort": "medium",       # 工数(思考の深さ)
        "top_k": settings.rag_top_k,
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "chunk_size": settings.chunk_size,
        "chunk_overlap": settings.chunk_overlap,
        "summarize_map_model": "",   # 一括要約の「下書き(map)」用モデル。空=メインと同じ(二段なし)
        "theme": "auto",
    }


ALLOWED_KEYS = set(base_defaults().keys())


def get_defaults() -> dict:
    d = base_defaults()
    stored = db.get_kv("defaults", {}) or {}
    for k, v in stored.items():
        if k in ALLOWED_KEYS and v is not None:
            d[k] = v
    return d


def set_defaults(patch: dict) -> dict:
    cur = db.get_kv("defaults", {}) or {}
    for k, v in patch.items():
        if k in ALLOWED_KEYS:
            cur[k] = v
    db.set_kv("defaults", cur)
    return get_defaults()


def effective_for(conv: dict) -> dict:
    """会話の実効パラメータ。"""
    d = get_defaults()
    if conv.get("model"):
        d["model"] = conv["model"]
    if conv.get("system_prompt"):
        d["system_prompt"] = conv["system_prompt"]
    s = conv.get("settings") or {}
    for k in ("temperature", "top_p", "num_predict", "num_ctx", "effort", "top_k"):
        if k in s and s[k] is not None:
            d[k] = s[k]
    return d


def chunk_params() -> tuple[int, int]:
    d = get_defaults()
    return int(d["chunk_size"]), int(d["chunk_overlap"])
