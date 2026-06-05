"""evalkit.metrics の単体テスト(RAG本体に依存しない)。

pytest でも `python tests/test_eval_metrics.py` 単体実行でも動く。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evalkit.metrics import (  # noqa: E402
    answer_contains,
    file_hit,
    first_hit_rank,
    summarize,
)


def test_file_hit_basename_and_case():
    assert file_hit(["就業規則.pdf"], ["/srv/docs/就業規則.pdf"]) is True
    assert file_hit(["Spec.XLSX"], ["spec.xlsx"]) is True
    assert file_hit(["a.pdf"], ["b.pdf", "c.pdf"]) is False


def test_file_hit_empty():
    assert file_hit([], ["a.pdf"]) is False
    assert file_hit(["a.pdf"], []) is False


def test_first_hit_rank():
    assert first_hit_rank(["b.pdf"], ["a.pdf", "b.pdf", "c.pdf"]) == 2
    assert first_hit_rank(["x.pdf"], ["a.pdf", "b.pdf"]) is None
    assert first_hit_rank(["a.pdf"], ["A.PDF"]) == 1   # 大文字小文字無視


def test_answer_contains():
    assert answer_contains("勤続6か月で10日付与", ["6か月", "10日"]) is True
    assert answer_contains("6か月", ["10日"]) is False
    assert answer_contains("any", []) is True          # 期待語句なし→True


def test_summarize():
    rows = [
        {"file_hit": True, "first_rank": 1, "answer_match": True},
        {"file_hit": True, "first_rank": 3, "answer_match": False},
        {"file_hit": False, "first_rank": None, "answer_match": None},
    ]
    s = summarize(rows)
    assert s["questions"] == 3
    assert s["file_hit_rate"] == round(2 / 3, 3)
    assert s["mean_first_rank"] == 2.0               # (1+3)/2、None は除外
    assert s["answer_match_rate"] == 0.5             # 2件中1件、None は除外


def test_summarize_empty():
    s = summarize([])
    assert s["questions"] == 0 and s["file_hit_rate"] is None


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
