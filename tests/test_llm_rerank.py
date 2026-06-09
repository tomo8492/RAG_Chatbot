"""retrieval.llm_rerank(LLMリランクの並べ替え・フォールバック・多様化)の単体テスト。

score_fn を注入する純関数なので、LLM/Ollama には接続しない。pytest でも
`python tests/test_llm_rerank.py` でも動く。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import retrieval   # noqa: E402


def test_llm_rerank_orders_by_score_desc():
    hits = [{"text": "a", "source": "x"}, {"text": "b", "source": "y"}, {"text": "c", "source": "z"}]
    out = retrieval.llm_rerank("q", hits, 3, lambda q, ts: [1.0, 9.0, 5.0])
    assert [h["text"] for h in out] == ["b", "c", "a"]


def test_llm_rerank_applies_top_k():
    hits = [{"text": str(i), "source": str(i)} for i in range(5)]
    out = retrieval.llm_rerank("q", hits, 2, lambda q, ts: [0, 1, 2, 3, 4])
    assert [h["text"] for h in out] == ["4", "3"]


def test_llm_rerank_fallback_on_length_mismatch():
    hits = [{"text": "a", "source": "x"}, {"text": "b", "source": "y"}]
    out = retrieval.llm_rerank("q", hits, 2, lambda q, ts: [1.0])   # 長さ不一致
    assert [h["text"] for h in out] == ["a", "b"]                   # 融合順を維持


def test_llm_rerank_fallback_on_exception():
    hits = [{"text": "a", "source": "x"}, {"text": "b", "source": "y"}]

    def boom(q, ts):
        raise RuntimeError("scorer down")

    out = retrieval.llm_rerank("q", hits, 2, boom)
    assert [h["text"] for h in out] == ["a", "b"]


def test_llm_rerank_diversifies_by_source():
    hits = [{"text": str(i), "source": "same"} for i in range(4)] + [{"text": "other", "source": "o"}]
    out = retrieval.llm_rerank("q", hits, 5, lambda q, ts: [9, 8, 7, 6, 1], max_per_source=2)
    srcs = [h["source"] for h in out]
    assert srcs.count("same") == 2 and "o" in srcs   # 1ソースの独占を抑制


def test_llm_rerank_empty():
    assert retrieval.llm_rerank("q", [], 5, lambda q, ts: []) == []


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
