"""app.ocr の設定参照(defaults 優先)の単体テスト。

needs_ocr / _ocr_model が「設定画面(defaults)→ .env(settings)」の順で値を解決するかを、
get_defaults をスタブして検証する(Ollama/外部には接続しない)。
"""
import contextlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import defaults, ocr   # noqa: E402


@contextlib.contextmanager
def fake_defaults(d):
    old = defaults.get_defaults
    defaults.get_defaults = lambda: d
    try:
        yield
    finally:
        defaults.get_defaults = old


def test_needs_ocr_off_when_defaults_false():
    with fake_defaults({"ocr_enabled": False}):
        assert ocr.needs_ocr("") is False


def test_needs_ocr_on_for_short_text_when_enabled():
    with fake_defaults({"ocr_enabled": True}):
        assert ocr.needs_ocr("") is True              # 空(<min_chars)→ OCR対象
        assert ocr.needs_ocr("x" * 100) is False      # 十分な文字数 → 不要


def test_ocr_model_prefers_ocr_vlm():
    with fake_defaults({"ocr_vlm_model": "glm-ocr", "vision_model": "gemma3"}):
        assert ocr._ocr_model() == "glm-ocr"


def test_ocr_model_falls_back_to_vision():
    with fake_defaults({"ocr_vlm_model": "", "vision_model": "gemma3"}):
        assert ocr._ocr_model() == "gemma3"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            passed += 1
        except AssertionError as e:
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"ERROR {fn.__name__}: {e!r}")
    print(f"{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
