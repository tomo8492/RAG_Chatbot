"""app.rag._doc_context(文脈付き埋め込み用の文脈生成)の単体テスト。

rag が重い依存(chromadb 等)で import できない環境では各テストを skip する
(独自ランナー方針に合わせ、LLM/Ollama は呼ばずスタブ化)。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from app import rag, llm   # noqa: E402
    _OK = True
except Exception:              # noqa: BLE001  (重依存欠如など)
    _OK = False


def test_doc_context_empty_text_returns_blank():
    if not _OK:
        print("SKIP test_doc_context_empty_text_returns_blank: rag import 不可")
        return
    assert rag._doc_context("m", "x.txt", "") == ""
    assert rag._doc_context("m", "x.txt", "   ") == ""


def test_doc_context_no_model_returns_blank():
    if not _OK:
        print("SKIP test_doc_context_no_model_returns_blank: rag import 不可")
        return
    assert rag._doc_context("", "x.txt", "本文あり") == ""


def test_doc_context_uses_llm_and_normalizes():
    if not _OK:
        print("SKIP test_doc_context_uses_llm_and_normalizes: rag import 不可")
        return
    old = llm.complete_text
    llm.complete_text = lambda prompt, model, **kw: "  これは規程文書です。\n対象は全社。  "
    try:
        out = rag._doc_context("m", "規程.pdf", "本文" * 100)
        assert out == "これは規程文書です。 対象は全社。"   # 余分な空白/改行は1スペースに正規化
    finally:
        llm.complete_text = old


def test_doc_context_llm_failure_returns_blank():
    if not _OK:
        print("SKIP test_doc_context_llm_failure_returns_blank: rag import 不可")
        return
    old = llm.complete_text

    def boom(*a, **k):
        raise RuntimeError("ollama down")

    llm.complete_text = boom
    try:
        assert rag._doc_context("m", "x.pdf", "本文") == ""   # 失敗時は従来動作(文脈なし)
    finally:
        llm.complete_text = old


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
