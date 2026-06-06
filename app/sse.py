"""sse.py
Server-Sent Events の整形ヘルパ(複数ルートで共有)。
"""
from __future__ import annotations

import json


def sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"
