"""app.llm の純粋ヘルパ(図意図検出・プロンプト整形・クエリ書き換えの判定)の単体テスト。

Ollama には接続しない経路だけを検証する(rewrite_query は非対象/モデル空で短絡)。
pytest でも `python tests/test_llm_helpers.py` 単体実行でも動く。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import llm  # noqa: E402

DETAIL_MARK = "stateDiagram-v2"   # 詳細な図ガイドにだけ現れる語
STYLE_MARK = "結論ファースト"        # STYLE_GUIDE は常に付与


def _sys(msgs):
    return msgs[0]["content"]


# ---------------- wants_diagram ----------------
def test_wants_diagram_true():
    for q in ("フローチャートで示して", "構成図を描いて", "mermaidで可視化して", "状態遷移を図示して"):
        assert llm.wants_diagram(q), q


def test_wants_diagram_false():
    for q in ("有給は何日もらえますか?", "経費精算の締め日はいつ?", "役職手当の金額を教えて"):
        assert not llm.wants_diagram(q), q


# ---------------- build_messages の図ガイド切替 ----------------
def test_build_messages_brief_when_no_diagram():
    s = _sys(llm.build_messages("", [], [], diagram_hint=False))
    assert DETAIL_MARK not in s and STYLE_MARK in s


def test_build_messages_full_when_diagram():
    s = _sys(llm.build_messages("", [], [], diagram_hint=True))
    assert DETAIL_MARK in s


def test_build_messages_full_when_none_backcompat():
    s = _sys(llm.build_messages("", [], [], diagram_hint=None))
    assert DETAIL_MARK in s          # 従来どおり詳細(後方互換)


def test_build_messages_includes_history():
    msgs = llm.build_messages("", [{"role": "user", "content": "やあ"},
                                   {"role": "assistant", "content": "はい"}], [])
    assert msgs[-2]["content"] == "やあ" and msgs[-1]["content"] == "はい"


# ---------------- should_rewrite_query ----------------
def test_should_rewrite_needs_prior_assistant():
    assert not llm.should_rewrite_query([{"role": "user", "content": "最初の質問"}], "その続きは?")


def test_should_rewrite_short_followup():
    hist = [{"role": "user", "content": "賞与について"}, {"role": "assistant", "content": "..."}]
    assert llm.should_rewrite_query(hist, "その金額は?")


def test_should_rewrite_long_without_hint_is_false():
    hist = [{"role": "assistant", "content": "..."}]
    q = "来年度の社員旅行の積立金の上限額について就業規則の規定を教えてください"
    assert not llm.should_rewrite_query(hist, q)


def test_should_rewrite_long_with_hint_is_true():
    hist = [{"role": "assistant", "content": "..."}]
    assert llm.should_rewrite_query(hist, "その制度についてもっと詳しく説明してください色々と")


# ---------------- build_rewrite_prompt ----------------
def test_build_rewrite_prompt_contains_context_and_query():
    hist = [{"role": "user", "content": "賞与の支給日は?"}, {"role": "assistant", "content": "6月と12月です"}]
    p = llm.build_rewrite_prompt(hist, "その金額は?")
    assert "賞与の支給日" in p and "6月と12月" in p
    assert "その金額は?" in p and "書き換え後の質問:" in p


# ---------------- rewrite_query(Ollamaを呼ばない経路) ----------------
def test_rewrite_query_passthrough_when_not_followup():
    # prior に assistant が無い → 書き換え対象外 → 原文のまま(Ollama未呼び出し)
    assert llm.rewrite_query([{"role": "user", "content": "x"}], "独立した質問", "some-model") == "独立した質問"


def test_rewrite_query_passthrough_when_model_empty():
    hist = [{"role": "assistant", "content": "..."}]
    assert llm.rewrite_query(hist, "その件", "") == "その件"


# ---------------- context_char_budget / trim_history(num_ctx 連動) ----------------
def test_context_char_budget_fallback_when_unknown():
    assert llm.context_char_budget(0) == llm.RAG_CONTEXT_CHAR_BUDGET
    assert llm.context_char_budget(-5) == llm.RAG_CONTEXT_CHAR_BUDGET


def test_context_char_budget_scales_and_caps():
    small = llm.context_char_budget(8192, 1024)
    large = llm.context_char_budget(32768, 1024)
    assert 1500 <= small < large <= llm.RAG_CONTEXT_CHAR_BUDGET   # num_ctx大→予算大・上限あり


def test_trim_history_count_and_chars():
    hist = [{"role": "user", "content": "x" * 100} for _ in range(50)]
    assert len(llm.trim_history(hist, max_messages=30, max_chars=0)) == 30   # 件数のみ
    out = llm.trim_history(hist, max_messages=30, max_chars=250)             # 文字数でも制限
    assert 1 <= len(out) <= 3 and sum(len(m["content"]) for m in out) <= 250


def test_build_messages_history_trimmed_by_small_num_ctx():
    hist = [{"role": "user", "content": "あ" * 500},
            {"role": "assistant", "content": "い" * 500}] * 10        # 20件×500字
    body = [m for m in llm.build_messages("", hist, [], num_ctx=8192, num_predict=1024)
            if m["role"] in ("user", "assistant")]
    assert 0 < len(body) < len(hist)        # 小さい num_ctx では直近一部だけ残す


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
