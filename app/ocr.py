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


def _ocr_enabled() -> bool:
    """OCR有効か。設定画面(defaults)があればそれを優先、無ければ .env(settings)。"""
    try:
        from .defaults import get_defaults
        v = get_defaults().get("ocr_enabled")
        if v is not None:
            return bool(v)
    except Exception:
        log.debug("_ocr_enabled: 例外を無視して継続", exc_info=True)
    return settings.ocr_enabled


def _ocr_model() -> str:
    """VLM-OCR に使うモデル。defaults の ocr_vlm_model → vision_model → settings の順。"""
    try:
        from .defaults import get_defaults
        d = get_defaults()
        return ((d.get("ocr_vlm_model") or "").strip()
                or (d.get("vision_model") or "").strip()
                or settings.vision_model)
    except Exception:
        log.debug("_ocr_model: 例外を無視して継続", exc_info=True)
        return settings.ocr_vlm_model or settings.vision_model


def needs_ocr(page_text: str) -> bool:
    """このページをOCRすべきか(OCR有効 かつ テキスト層が閾値未満=スキャン頁)。"""
    if not _ocr_enabled():
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

# 文書内の挿図(図1等)を検索で見つけられるようにするための説明生成指示
_DESCRIBE_PROMPT = (
    "この画像は社内文書に挿入された図です。検索の手がかりになるよう:\n"
    "1) 図の種類(写真/画面/配線図/フロー図/グラフ等)と内容を日本語で1〜2文で説明する\n"
    "2) 図中の重要な文字・ラベル・番号があれば列挙する\n"
    "前置きや感想は書かず、説明だけを簡潔に返してください。"
)


# 画像非対応と判明したOCRモデルを記録し、ビルド中の「全ページで失敗」連打を防ぐ。
_vlm_blocked: dict[str, str] = {}


def _model_has_vision(model: str) -> bool:
    """OCRモデルが画像対応(vision)か。能力情報が取れないときは True(従来どおり試す=安全側)。"""
    if not model:
        return False
    try:
        from . import llm
        caps = llm.model_capabilities(model)
        if caps:                         # 能力情報が取れたときだけ判定する
            return "vision" in caps
    except Exception:
        log.debug("_model_has_vision: 例外を無視して継続", exc_info=True)
    return True                          # 不明 → 試す(実呼び出しの例外側で捕捉)


def _looks_like_no_vision(msg: str) -> bool:
    """Ollama の「画像非対応」系エラーメッセージか。"""
    m = (msg or "").lower()
    return any(k in m for k in ("image input is not supported", "mmproj",
                                "multimodal requests", "does not support multimodal"))


def _block_model(model: str, reason: str) -> None:
    """画像非対応モデルを記録し、初回だけ明確に警告する(以降は呼ばずにスキップ)。"""
    if model and model not in _vlm_blocked:
        _vlm_blocked[model] = reason
        log.warning("OCRをスキップ: %s", reason)


def reset_run_state() -> None:
    """インデックス構築の開始時に呼ぶ。画像非対応モデルの一時ブロックを解除して再判定させる。"""
    _vlm_blocked.clear()


def ocr_image_png(png: bytes) -> str:
    """ページ画像(PNGバイト列)をOCRしてテキスト/Markdownを返す。失敗時は ''。"""
    engine = settings.ocr_engine
    if engine != "tesseract":
        model = _ocr_model()
        if model in _vlm_blocked:        # 画像非対応と判明済み → 呼ばずに即スキップ(連打を防ぐ)
            return ""
        if not _model_has_vision(model):
            _block_model(model, f"OCRモデル『{model}』は画像非対応(visionなし)。"
                                "設定で qwen2.5vl など画像対応モデルを選んでください")
            return ""
    try:
        if engine == "tesseract":
            return _ocr_tesseract(png)
        return _ocr_vlm(png)
    except Exception as e:  # 未導入・モデル無し・実行時エラー → 縮退
        emsg = str(e)
        if engine != "tesseract" and _looks_like_no_vision(emsg):
            m = _ocr_model()
            _block_model(m, f"OCRモデル『{m}』が画像入力に未対応(mmproj無し等)。"
                            "設定で qwen2.5vl など画像対応モデルを選んでください")
        else:
            log.warning("OCR失敗(engine=%s): %s", engine, emsg[:200])
        return ""


def describe_image_png(png: bytes) -> str:
    """文書内の図(画像)の検索用説明文を生成する(図チャンクの索引化に使う)。

    OCRと同じエンジン/モデル設定を流用する。OCR無効・モデル非対応・失敗時は ''
    (=図チャンクを作らないだけで、画像のリンク・表示には影響しない)。
    """
    if not _ocr_enabled():
        return ""
    if settings.ocr_engine == "tesseract":
        try:
            return _ocr_tesseract(png)   # 説明はできないが、図中の文字は検索に乗せられる
        except Exception:
            log.debug("describe_image_png: tesseract失敗(無視)", exc_info=True)
            return ""
    model = _ocr_model()
    if model in _vlm_blocked:
        return ""
    if not _model_has_vision(model):
        _block_model(model, f"OCRモデル『{model}』は画像非対応(visionなし)。"
                            "設定で qwen2.5vl など画像対応モデルを選んでください")
        return ""
    try:
        return _vlm_chat(png, _DESCRIBE_PROMPT)
    except Exception as e:
        emsg = str(e)
        if _looks_like_no_vision(emsg):
            _block_model(model, f"OCRモデル『{model}』が画像入力に未対応(mmproj無し等)。"
                                "設定で qwen2.5vl など画像対応モデルを選んでください")
        else:
            log.warning("図の説明生成に失敗: %s", emsg[:200])
        return ""


def _vlm_chat(png: bytes, prompt: str) -> str:
    import ollama

    model = _ocr_model()
    b64 = base64.b64encode(png).decode("ascii")
    client = ollama.Client(host=settings.ollama_host)
    resp = client.chat(
        model=model,
        messages=[{"role": "user", "content": prompt, "images": [b64]}],
        options={"temperature": 0.0},
    )
    return (resp.get("message", {}).get("content") or "").strip()


def _ocr_vlm(png: bytes) -> str:
    return _vlm_chat(png, _VLM_PROMPT)


def _ocr_tesseract(png: bytes) -> str:
    import pytesseract
    from PIL import Image

    img = Image.open(io.BytesIO(png))
    return pytesseract.image_to_string(img, lang=settings.ocr_lang).strip()


def ocr_available() -> tuple[bool, str]:
    """選択中のエンジンが使えるか(導入チェック)。(ok, 理由) を返す。"""
    if not _ocr_enabled():
        return False, "OCR無効(設定画面でOFF または OCR_ENABLED=false)"
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

        model = _ocr_model()
        if not model:
            return False, "VISION_MODEL/OCR_VLM_MODEL 未設定"
        if not llm.is_model_installed(model):
            return False, f"VLモデル未導入: {model}(ollama pull が必要)"
        if not _model_has_vision(model):
            return False, f"モデル『{model}』は画像非対応(visionなし)。qwen2.5vl 等を選択してください"
        return True, f"vlm:{model}"
    except Exception as e:
        log.debug("ocr_available: 例外を無視して継続", exc_info=True)
        return False, f"vlm確認失敗: {str(e)[:120]}"
