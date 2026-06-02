#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run.py
アプリ起動スクリプト。

    python run.py

設定は .env(無ければ既定値)から読み込みます。
HOST=0.0.0.0 にすると同一ネットワークの他PCのブラウザからアクセスできます。
"""
from __future__ import annotations

import socket

import uvicorn

from app.config import settings
from app.logging_setup import setup_logging, get_logger


def _local_ip() -> str:
    """LAN内の自分のIPを推定(他PCからのアクセスURL表示用)。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def main() -> None:
    setup_logging()
    log = get_logger("run")
    ip = _local_ip()
    log.info("=" * 56)
    log.info(" %s", settings.app_title)
    log.info("=" * 56)
    log.info(" このPC      : http://localhost:%s", settings.port)
    if settings.host == "0.0.0.0":
        log.info(" 他のPCから  : http://%s:%s", ip, settings.port)
    log.info(" 認証        : %s", "有効" if settings.auth_enabled else "無効(パスワード未設定)")
    log.info(" Ollama      : %s / モデル %s", settings.ollama_host, settings.chat_model)
    log.info("=" * 56)

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
