"""
logging_setup.py
すべての処理ログとエラーをターミナルに出力する(旧Tkinter版の方針を踏襲)。
リクエストID(相関ID)をログに付与し、同時アクセス時でも1操作を追跡できるようにする。
"""
from __future__ import annotations

import contextvars
import logging
import sys

_CONFIGURED = False

# リクエスト相関ID。ミドルウェアが設定し、全ログ行へ付与する(未設定時は "-")。
_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


def set_request_id(rid: str) -> None:
    _request_id.set(rid)


def get_request_id() -> str:
    return _request_id.get()


class _RequestIdFilter(logging.Filter):
    """全ログレコードに request_id を付与する(書式 %(request_id)s 用)。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id.get()
        return True


def setup_logging(level: int = logging.INFO) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_RequestIdFilter())
    handler.setFormatter(
        logging.Formatter(
            fmt="[%(asctime)s] [%(levelname)s] %(name)s [%(request_id)s]: %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)

    # 依存ライブラリの過剰なログは抑制
    for noisy in ("httpx", "httpcore", "chromadb", "sentence_transformers", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)
