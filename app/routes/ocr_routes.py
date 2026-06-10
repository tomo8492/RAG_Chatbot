"""ocr_routes.py
OCR API(VBA / Python など外部から呼ぶ用)。
  画像パス(または base64)+ 指示文 を受け取り、Vision モデルの応答を返す。
  例) {"path": "C:/work/伝票.png", "instruction": "購入数量を数字だけで返信"}
認証は API キー(X-API-Key)または ログインセッションのどちらかを要求する。
"""
from __future__ import annotations

import base64
import hmac
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Cookie, Header, HTTPException
from pydantic import BaseModel

from .. import auth, llm
from ..config import settings
from ..logging_setup import get_logger

log = get_logger("ocr")

DEFAULT_OCR_INSTRUCTION = (
    "この画像に書かれている文字をすべて正確に読み取り、本文だけを出力してください。"
    "前置き・説明・注釈は不要です。"
)
_OCR_IMG_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}

router = APIRouter()


class OcrBody(BaseModel):
    path: str = ""                  # サーバ上の画像ファイルパス
    image_b64: str = ""             # path の代わりに base64 / data URL を直接渡す場合
    instruction: str = ""           # 読み取り後の指示(空なら全文OCR)
    model: str = ""                 # 使用モデル(空なら設定のVisionモデル)
    num_predict: int = 512          # 応答の最大トークン
    temperature: float = 0.1


def _check_ocr_auth(x_api_key: Optional[str], rag_session: Optional[str]) -> None:
    """OCR API の認証。認証無効時は素通り。有効時は API キー か セッションCookie を要求。"""
    if not settings.auth_enabled:
        return
    if (settings.ocr_api_key and x_api_key
            and hmac.compare_digest(x_api_key, settings.ocr_api_key)):
        return
    if auth.is_authenticated(rag_session):
        return
    raise HTTPException(401, "認証が必要です(X-API-Key ヘッダ、またはログインセッションが必要)")


@router.post("/api/ocr")
def api_ocr(body: OcrBody,
            x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
            rag_session: Optional[str] = Cookie(default=None)) -> dict:
    _check_ocr_auth(x_api_key, rag_session)

    # --- 画像の取得(base64 優先、無ければパスから読込) ---
    if body.image_b64.strip():
        b64 = body.image_b64.strip()
        if b64.startswith("data:"):
            try:
                b64 = b64.split(",", 1)[1]
            except IndexError:
                raise HTTPException(400, "data URL の形式が不正です")
    elif body.path.strip():
        p = Path(body.path.strip())
        if not p.is_file():
            raise HTTPException(404, f"ファイルが見つかりません: {p}")
        if p.suffix.lower() not in _OCR_IMG_SUFFIXES:
            raise HTTPException(400, f"対応していない画像形式です: {p.suffix}")
        data = p.read_bytes()
        if len(data) > settings.max_upload_mb * 1024 * 1024:
            raise HTTPException(413, f"画像が大きすぎます(上限 {settings.max_upload_mb}MB)")
        b64 = base64.b64encode(data).decode("ascii")
    else:
        raise HTTPException(400, "path か image_b64 のどちらかを指定してください")

    # --- モデル確認 ---
    model = llm.resolve_installed((body.model or settings.vision_model or "").strip())
    if not model:
        raise HTTPException(400, "Vision モデルが未設定です(model 指定か VISION_MODEL 設定が必要)")
    if not llm.is_ollama_available():
        raise HTTPException(503, f"Ollama に接続できません({settings.ollama_host})")
    if not llm.is_model_installed(model):
        raise HTTPException(400, f"モデル『{model}』が見つかりません。`ollama pull {model}` を実行してください。")

    instruction = body.instruction.strip() or DEFAULT_OCR_INSTRUCTION
    log.info("OCR要求 [model=%s src=%s] instruction=%s",
             model, (body.path or "(base64)")[:80], instruction[:60])
    try:
        text = llm.vision_complete([b64], instruction, model,
                                   temperature=float(body.temperature),
                                   num_predict=int(body.num_predict))
    except Exception as e:
        msg = str(e)
        if "image input is not supported" in msg.lower():
            raise HTTPException(
                400,
                f"モデル『{model}』は画像入力に対応していません(Vision/mmproj 非対応)。"
                "qwen2.5vl など画像対応モデルを model に指定するか VISION_MODEL に設定してください。")
        log.exception("OCR失敗")
        raise HTTPException(500, f"OCR処理に失敗しました: {msg}")

    return {"ok": True, "model": model, "result": (text or "").strip()}
