"""チャットの選択式聞き返し(曖昧クエリ検知)のテスト。

_maybe_clarify_sources の発動条件・抑止条件と、focus_source による絞り込みを検証。
LLM・Chroma には接続しない(純ロジック)。pytest でも単体実行でも動く。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import (                      # noqa: E402
    _CLARIFY_HEAD, _apply_focus_source, _maybe_clarify_sources)


def _hit(source, score=0.8, loc="p.1", attachment=False):
    return {"source": source, "score": score, "loc": loc, "attachment": attachment}


def _ambiguous_hits():
    """3資料に割れた上位ヒット(スコア差は小さい=どれも同程度)。"""
    return [
        _hit("出張旅費規程.docx", 0.80, "第3章 日当"),
        _hit("経費精算マニュアル.xlsx", 0.79, "シート:精算"),
        _hit("就業規則.pdf", 0.77, "p.12"),
        _hit("出張旅費規程.docx", 0.76, "第4章"),
    ]


# ---------------- 発動するケース ----------------
def test_clarify_triggers_on_short_ambiguous_query():
    c = _maybe_clarify_sources("日当はいくら?", _ambiguous_hits())
    assert c is not None
    labels = [o["label"] for o in c["options"]]
    assert labels[0] == "出張旅費規程.docx" and len(labels) == 3   # 出現順・重複なし
    assert c["text"].startswith(_CLARIFY_HEAD)
    assert "第3章 日当" in c["options"][0]["description"]


# ---------------- 抑止するケース ----------------
def test_no_clarify_for_specific_long_query():
    q = "出張のときの日当は1日あたりいくら支給されますか?"   # 24字超=具体的
    assert _maybe_clarify_sources(q, _ambiguous_hits()) is None


def test_no_clarify_when_single_or_two_sources():
    hits = [_hit("A.pdf", 0.8), _hit("A.pdf", 0.78), _hit("B.pdf", 0.7)]
    assert _maybe_clarify_sources("日当は?", hits) is None       # 2資料では聞かない


def test_no_clarify_when_top_source_dominant():
    hits = [_hit("A.pdf", 0.90), _hit("B.pdf", 0.70), _hit("C.pdf", 0.69)]
    assert _maybe_clarify_sources("日当は?", hits) is None       # 先頭が明確に優勢


def test_no_clarify_when_filename_in_query():
    assert _maybe_clarify_sources("出張旅費規程の日当は?", _ambiguous_hits()) is None


def test_no_clarify_twice_in_a_row():
    prev = _CLARIFY_HEAD + "候補が複数の資料に…"
    assert _maybe_clarify_sources("日当は?", _ambiguous_hits(), prev) is None


def test_attachment_hits_do_not_count_as_sources():
    hits = [_hit("貼った資料.pdf", 0.9, attachment=True),
            _hit("A.pdf", 0.8), _hit("B.pdf", 0.79)]
    assert _maybe_clarify_sources("これは?", hits) is None       # 実資料は2つだけ


def test_no_clarify_on_empty():
    assert _maybe_clarify_sources("", _ambiguous_hits()) is None
    assert _maybe_clarify_sources("日当は?", []) is None


# ---------------- focus_source の絞り込み ----------------
def test_apply_focus_source_filters_hits():
    hits = _ambiguous_hits()
    got = _apply_focus_source(hits, "出張旅費規程.docx")
    assert len(got) == 2 and all(h["source"] == "出張旅費規程.docx" for h in got)
    assert _apply_focus_source(hits, "存在しない.pdf") == []      # 呼び出し側で全件継続
    assert _apply_focus_source(hits, "") == []


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
