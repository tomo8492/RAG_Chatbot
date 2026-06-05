"""OCRルーティングの単体テスト。

OCRエンジン(VLM/tesseract)自体は実機依存のためモックし、
「テキスト頁はそのまま・スキャン頁(テキスト層なし)だけOCRへ」という分岐を検証する。
pytest でも `python tests/test_ocr.py` でも動く。
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import loaders, ocr  # noqa: E402
from app.config import settings  # noqa: E402

try:
    import fitz  # noqa: F401  (PyMuPDF)
    _HAS_FITZ = True
except Exception:
    _HAS_FITZ = False


class _Skip(Exception):
    pass


def _skip(msg: str):
    """pytest があれば skip、なければ _Skip(単体ランナーが skipped 扱い)。"""
    try:
        import pytest
        pytest.skip(msg)
    except ImportError:
        raise _Skip(msg)


def _make_pdf(with_text: bool) -> Path:
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    if with_text:
        page.insert_text((72, 72), "契約書 Agreement 2024 ABC-123")
    tmp = Path(tempfile.mkdtemp()) / ("text.pdf" if with_text else "scan.pdf")
    doc.save(str(tmp))
    doc.close()
    return tmp


def _restore():
    settings.ocr_enabled = False


# ---------------- needs_ocr ----------------
def test_needs_ocr_off_by_default():
    settings.ocr_enabled = False
    assert ocr.needs_ocr("") is False
    assert ocr.needs_ocr("x" * 100) is False


def test_needs_ocr_when_enabled():
    try:
        settings.ocr_enabled = True
        settings.ocr_min_chars = 16
        assert ocr.needs_ocr("") is True            # テキスト層なし → OCR
        assert ocr.needs_ocr("少しだけ") is True       # 閾値未満 → OCR
        assert ocr.needs_ocr("十分な長さの本文テキストがここにあります") is False  # 閾値以上 → そのまま
    finally:
        _restore()


def test_ocr_available_disabled():
    settings.ocr_enabled = False
    ok, reason = ocr.ocr_available()
    assert ok is False and "false" in reason.lower()


# ---------------- ルーティング(fitz非依存・全分岐) ----------------
class _FakePixmap:
    def tobytes(self, fmt):
        return b"PNGDATA"


class _FakePage:
    def __init__(self, text="", pixmap_raises=False):
        self._text, self._raises = text, pixmap_raises

    def get_text(self):
        return self._text

    def get_pixmap(self, dpi=200):
        if self._raises:
            raise RuntimeError("render fail")
        return _FakePixmap()


def _route(page):
    return loaders._page_text_with_ocr(page, "x.pdf", 1)


def test_route_text_page_skips_ocr():
    called = {"n": 0}
    orig = ocr.ocr_image_png
    try:
        settings.ocr_enabled = True
        ocr.ocr_image_png = lambda png: (called.__setitem__("n", called["n"] + 1) or "NO")
        out = _route(_FakePage("十分な長さの本文テキストがあります"))
        assert out == "十分な長さの本文テキストがあります"
        assert called["n"] == 0          # テキスト頁はOCRしない
    finally:
        ocr.ocr_image_png = orig
        _restore()


def test_route_scanned_uses_ocr():
    orig = ocr.ocr_image_png
    try:
        settings.ocr_enabled = True
        ocr.ocr_image_png = lambda png: "OCR本文 | A | B |"
        assert _route(_FakePage("")) == "OCR本文 | A | B |"
    finally:
        ocr.ocr_image_png = orig
        _restore()


def test_route_disabled_no_ocr():
    settings.ocr_enabled = False
    assert _route(_FakePage("")) == ""        # OCR無効→従来どおり空


def test_route_ocr_empty_keeps_short_text():
    """短いテキスト頁でOCRが空を返したら、元の短いテキストを失わない。"""
    orig = ocr.ocr_image_png
    try:
        settings.ocr_enabled = True
        ocr.ocr_image_png = lambda png: ""
        assert _route(_FakePage("短い")) == "短い"
    finally:
        ocr.ocr_image_png = orig
        _restore()


def test_route_pixmap_error_degrades():
    """画像化(get_pixmap)が例外でも落ちず、OCRせず元テキストにフォールバック。"""
    orig = ocr.ocr_image_png
    try:
        settings.ocr_enabled = True
        ocr.ocr_image_png = lambda png: "SHOULD_NOT_REACH"
        assert _route(_FakePage("", pixmap_raises=True)) == ""   # 例外→ocr_text=""→空
    finally:
        ocr.ocr_image_png = orig
        _restore()


# ---------------- load_pdf ルーティング ----------------
def test_text_pdf_uses_textlayer_no_ocr():
    """テキストPDFは、OCR有効でもOCRを呼ばずテキスト層を使う。"""
    if not _HAS_FITZ:
        _skip("PyMuPDF未導入のためskip")
    called = {"n": 0}
    orig = ocr.ocr_image_png
    try:
        settings.ocr_enabled = True
        ocr.ocr_image_png = lambda png: (called.__setitem__("n", called["n"] + 1) or "SHOULD_NOT_USE")
        docs = loaders.load_pdf(_make_pdf(with_text=True))
        assert len(docs) == 1
        assert "Agreement" in docs[0]["text"] and "ABC-123" in docs[0]["text"]
        assert called["n"] == 0                      # OCRは呼ばれない
        assert docs[0]["loc"] == "p.1"
    finally:
        ocr.ocr_image_png = orig
        _restore()


def test_scanned_pdf_routes_to_ocr():
    """テキスト層が無い頁は OCR 結果を本文に採用する。"""
    if not _HAS_FITZ:
        _skip("PyMuPDF未導入のためskip")
    orig = ocr.ocr_image_png
    try:
        settings.ocr_enabled = True
        ocr.ocr_image_png = lambda png: "OCRで読んだ本文\n| 品目 | 数量 |\n| 鋼材 | 10 |"
        docs = loaders.load_pdf(_make_pdf(with_text=False))
        assert len(docs) == 1
        assert "OCRで読んだ本文" in docs[0]["text"]
        assert "| 品目 | 数量 |" in docs[0]["text"]    # 表(Markdown)が保持される
    finally:
        ocr.ocr_image_png = orig
        _restore()


def test_scanned_pdf_skips_when_ocr_off():
    """OCR無効なら従来どおり、テキストの無い頁は空(skip)。"""
    if not _HAS_FITZ:
        _skip("PyMuPDF未導入のためskip")
    settings.ocr_enabled = False
    docs = loaders.load_pdf(_make_pdf(with_text=False))
    assert docs == []


def test_ocr_failure_degrades_gracefully():
    """OCRが空を返しても落ちず、該当頁を skip する(従来動作)。"""
    if not _HAS_FITZ:
        _skip("PyMuPDF未導入のためskip")
    orig = ocr.ocr_image_png
    try:
        settings.ocr_enabled = True
        ocr.ocr_image_png = lambda png: ""           # エンジン未導入等で空
        docs = loaders.load_pdf(_make_pdf(with_text=False))
        assert docs == []
    finally:
        ocr.ocr_image_png = orig
        _restore()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = skipped = failed = 0
    for fn in fns:
        try:
            fn()
            passed += 1
        except _Skip as e:
            skipped += 1
            print(f"SKIP {fn.__name__}: {e}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {fn.__name__}: {e!r}")
    print(f"{passed} passed, {skipped} skipped, {failed} failed / {len(fns)}")
    sys.exit(0 if failed == 0 else 1)
