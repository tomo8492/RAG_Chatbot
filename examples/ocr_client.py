"""
ocr_client.py
/api/ocr を呼び出す Python クライアント兼CLI。

使い方:
    python examples/ocr_client.py "C:/work/伝票.png"
    python examples/ocr_client.py "C:/work/伝票.png" --instruction "購入数量を半角数字だけで返信"
    python examples/ocr_client.py img.png -i "合計金額を数字だけで" --model qwen2.5vl:7b

環境変数:
    OCR_BASE_URL  既定 http://localhost:8800
    OCR_API_KEY   認証を有効にしている場合に X-API-Key として送る値
"""
from __future__ import annotations

import argparse
import os
import sys

import requests


def ocr(path: str, instruction: str = "", model: str = "",
        base_url: str = "http://localhost:8800", api_key: str = "",
        timeout: int = 180) -> str:
    """画像パスと指示文を送り、結果テキストを返す。"""
    headers = {"X-API-Key": api_key} if api_key else {}
    resp = requests.post(
        f"{base_url.rstrip('/')}/api/ocr",
        headers=headers,
        json={"path": path, "instruction": instruction, "model": model},
        timeout=timeout,
    )
    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        raise RuntimeError(f"OCR APIエラー {resp.status_code}: {detail}")
    return resp.json().get("result", "")


def main() -> int:
    ap = argparse.ArgumentParser(description="OCR API クライアント")
    ap.add_argument("path", help="画像ファイルのパス(サーバから見えるパス)")
    ap.add_argument("-i", "--instruction", default="",
                    help="読み取り後の指示。例: '購入数量を半角数字だけで返信'")
    ap.add_argument("-m", "--model", default="", help="使用モデル(省略可)")
    ap.add_argument("--base-url", default=os.getenv("OCR_BASE_URL", "http://localhost:8800"))
    ap.add_argument("--api-key", default=os.getenv("OCR_API_KEY", ""))
    args = ap.parse_args()

    try:
        print(ocr(args.path, args.instruction, args.model, args.base_url, args.api_key))
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
