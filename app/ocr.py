"""app/ocr.py — スキャン/画像PDFページのOCR。

PDFはページ単位でテキスト層の有無を判定し、無い(=スキャン)ページだけを画像化してOCRする。
エンジンは VLM(Ollama のビジョンモデル・既定)/ pytesseract から選択。
すべて OCR_ENABLED 等のフラグで制御し、未導入・失敗時は空文字を返して従来動作
(該当ページを skip)に縮退する(クラッシュさせない)。日英(jpn+eng)対応。
"""
from __future__ import annotations

import base64
import io

from .config import settings
from .logging_setup import get_logger

log = get_logger("ocr")


def needs_ocr(page_text: str) -> bool:
    """このページをOCRすべきか(OCR有効 かつ テキスト層が閾値未満=スキャン頁)。"""
    if not settings.ocr_enabled:
        return False
    return len((page_text or "").strip()) < settings.ocr_min_chars


# VLM への文字起こし指示(忠実・表はMarkdown・日英・余計な前置きなし)
_VLM_PROMPT = (
    "この画像はPDFのスキャンページです。書かれているテキストを忠実に文字起こししてください。\n"
    "- 日本語・英語の両方に対応する。\n"
    "- 表は Markdown の表(| 列 | 列 |)で、行と列の構造を保ったまま書き出す。\n"
    "- 見出し・段落の改行は保持し、ページに無い内容は足さない。\n"
    "- 説明や前置きは書かず、ページの本文だけを返す。"
)


def ocr_image_png(png: bytes) -> str:
    """ページ画像(PNGバイト列)をOCRしてテキスト/Markdownを返す。失敗時は ''。"""
    engine = settings.ocr_engine
    try:
        if engine == "tesseract":
            return _ocr_tesseract(png)
        return _ocr_vlm(png)
    except Exception as e:  # 未導入・モデル無し・実行時エラー → 縮退
        log.warning("OCR失敗(engine=%s): %s", engine, str(e)[:200])
        return ""


def _ocr_vlm(png: bytes) -> str:
    import ollama

    model = settings.ocr_vlm_model or settings.vision_model
    b64 = base64.b64encode(png).decode("ascii")
    client = ollama.Client(host=settings.ollama_host)
    resp = client.chat(
        model=model,
        messages=[{"role": "user", "content": _VLM_PROMPT, "images": [b64]}],
        options={"temperature": 0.0},
    )
    return (resp.get("message", {}).get("content") or "").strip()


def _ocr_tesseract(png: bytes) -> str:
    import pytesseract
    from PIL import Image

    img = Image.open(io.BytesIO(png))
    return pytesseract.image_to_string(img, lang=settings.ocr_lang).strip()


def ocr_available() -> tuple[bool, str]:
    """選択中のエンジンが使えるか(導入チェック)。(ok, 理由) を返す。"""
    if not settings.ocr_enabled:
        return False, "OCR_ENABLED=false"
    if settings.ocr_engine == "tesseract":
        try:
            import pytesseract

            pytesseract.get_tesseract_version()
            return True, f"tesseract(lang={settings.ocr_lang})"
        except Exception as e:
            log.debug("ocr_available: 例外を無視して継続", exc_info=True)
            return False, f"tesseract未導入: {str(e)[:120]}"
    # vlm
    try:
        from . import llm

        model = settings.ocr_vlm_model or settings.vision_model
        if not model:
            return False, "VISION_MODEL/OCR_VLM_MODEL 未設定"
        ok = llm.is_model_installed(model)
        return ok, f"vlm:{model}" if ok else f"VLモデル未導入: {model}(ollama pull が必要)"
    except Exception as e:
        log.debug("ocr_available: 例外を無視して継続", exc_info=True)
        return False, f"vlm確認失敗: {str(e)[:120]}"
