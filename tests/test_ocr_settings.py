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


def test_model_has_vision_detects_capability():
    import app.llm as llm_mod
    old = llm_mod.model_capabilities
    try:
        llm_mod.model_capabilities = lambda m: ["completion", "vision"]
        assert ocr._model_has_vision("m") is True
        llm_mod.model_capabilities = lambda m: ["completion"]        # vision 無し
        assert ocr._model_has_vision("m") is False
        llm_mod.model_capabilities = lambda m: []                    # 不明 → 試す(安全側 True)
        assert ocr._model_has_vision("m") is True
    finally:
        llm_mod.model_capabilities = old


def test_ocr_image_png_skips_non_vision_without_calling_model():
    """画像非対応モデルなら _ocr_vlm を呼ばずに即 ''(全ページ失敗の連打をしない)。"""
    import app.llm as llm_mod
    ocr.reset_run_state()
    old_caps, old_vlm, old_model = llm_mod.model_capabilities, ocr._ocr_vlm, ocr._ocr_model
    called = {"n": 0}
    try:
        ocr._ocr_model = lambda: "fakevlm"
        llm_mod.model_capabilities = lambda m: ["completion"]        # vision 無し
        ocr._ocr_vlm = lambda png: called.__setitem__("n", called["n"] + 1) or "X"
        assert ocr.ocr_image_png(b"PNGDATA") == ""
        assert ocr.ocr_image_png(b"PNGDATA") == ""                   # 2回目はブロック済みで即スキップ
        assert called["n"] == 0                                      # モデルは一度も呼ばれない
    finally:
        llm_mod.model_capabilities, ocr._ocr_vlm, ocr._ocr_model = old_caps, old_vlm, old_model
        ocr.reset_run_state()


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
