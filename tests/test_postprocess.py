"""app.postprocess の単体テスト。

pytest でも、`python tests/test_postprocess.py` 単体実行でも動く。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.postprocess import (  # noqa: E402
    _balance_brackets,
    clean,
    close_unclosed_fence,
    fix_spelling,
    normalize_mermaid,
    strip_think,
    validate_mermaid,
)


# ---------------- strip_think ----------------
def test_strip_complete_block_keeps_surrounding():
    assert strip_think("前<think>考え中</think>後") == "前後"


def test_strip_multiline_block():
    assert strip_think("A<think>\n複数\n行\n</think>B") == "AB"


def test_strip_multiple_blocks():
    assert strip_think("a<think>x</think>b<think>y</think>c") == "abc"


def test_strip_open_only_removes_tail():
    # 閉じが無い(ストリーミング途中 or 崩れ): <think> 以降を捨てる
    assert strip_think("答え<think>まだ考えている途中") == "答え"


def test_strip_close_only_removes_head():
    # 開きが無く </think> だけ: 先頭〜閉じまでを思考とみなして捨てる
    assert strip_think("内部の推論…</think>\n本当の答え").strip() == "本当の答え"


def test_strip_case_insensitive_and_spaces():
    assert strip_think("x<THINK>a</think >y") == "xy"
    assert strip_think("x<think >a</THINK>y") == "xy"


def test_no_think_unchanged():
    s = "通常のテキスト\n- a\n- b"
    assert strip_think(s) == s


def test_strip_preserves_mermaid_block():
    src = "図:\n```mermaid\nflowchart TD\nA-->B\n```"
    assert strip_think("<think>悩む</think>" + src) == src


def test_strip_empty():
    assert strip_think("") == ""
    assert strip_think(None) == ""


# ---------------- close_unclosed_fence ----------------
def test_fence_balanced_unchanged():
    s = "```python\nx=1\n```"
    assert close_unclosed_fence(s) == s


def test_fence_unclosed_gets_closed():
    out = close_unclosed_fence("```mermaid\nflowchart TD\nA-->B")
    assert out.endswith("```")
    assert len(__import__("re").findall(r"(?m)^[ \t]*```", out)) == 2


def test_fence_none_unchanged():
    s = "ただの文章"
    assert close_unclosed_fence(s) == s


# ---------------- validate_mermaid ----------------
def test_valid_mermaid_no_issues():
    assert validate_mermaid("```mermaid\nflowchart TD\nA-->B\n```") == []


def test_mermaid_missing_declaration():
    issues = validate_mermaid("```mermaid\nA-->B\n```")
    assert any("図種宣言" in i for i in issues)


def test_mermaid_unclosed():
    issues = validate_mermaid("```mermaid\nsequenceDiagram\nA->>B: hi")
    assert any("閉じられて" in i for i in issues)


def test_validate_multiple_blocks():
    text = "```mermaid\nflowchart TD\nA-->B\n```\n間\n```mermaid\nbad line\n```"
    issues = validate_mermaid(text)
    assert any("図種宣言" in i for i in issues)


# ---------------- clean ----------------
def test_clean_strips_think_and_closes_fence():
    out = clean("<think>reason</think>結果:\n```mermaid\nflowchart TD\nA-->B")
    assert "<think>" not in out and "reason" not in out
    assert out.startswith("結果:")
    assert out.rstrip().endswith("```")


def test_clean_strict_strict_not_found_message_untouched():
    s = "参考資料内には、その内容に関する記載が見つかりませんでした。"
    assert clean(s) == s


# ---------------- spelling / normalize_mermaid ----------------
def test_fix_spelling_basic():
    assert fix_spelling("Srart") == "Start"
    assert fix_spelling("undefine?") == "undefined?"
    assert fix_spelling("Set Defualt Vaule") == "Set Default Value"


def test_fix_spelling_keeps_correct_words():
    assert fix_spelling("undefined") == "undefined"   # 正しい語は変えない
    assert fix_spelling("startup") == "startup"        # 部分一致で壊さない


def test_normalize_only_inside_mermaid():
    text = "本文に Srart と書いても直さない。\n\n```mermaid\nflowchart TD\n  A[Srart]:::startend --> B[undefine?]\n```"
    out = normalize_mermaid(text)
    assert "本文に Srart" in out                       # 本文は不変
    assert "A[Start]:::startend" in out                # 図内は補正
    assert "B[undefined?]" in out


def test_normalize_handles_unclosed_block():
    out = normalize_mermaid("```mermaid\nflowchart TD\n  A[Srart]")
    assert "A[Start]" in out


def test_clean_fixes_spelling_in_mermaid():
    out = clean("<think>x</think>図:\n```mermaid\nflowchart TD\n  A[Srart]:::startend --> B[Defualt]")
    assert "A[Start]" in out and "B[Default]" in out
    assert out.rstrip().endswith("```")               # 未閉じも閉じる


# ---------------- _balance_brackets(flowchart 括弧補修) ----------------
def test_balance_fixes_mismatched_close():
    assert _balance_brackets("  E --> F[問題発覚?}") == "  E --> F[問題発覚?]"


def test_balance_closes_truncated_node():
    assert _balance_brackets("  L --> M[手順書・特殊工程仕様書承") == "  L --> M[手順書・特殊工程仕様書承]"


def test_balance_keeps_valid_lines_unchanged():
    for ln in ("flowchart LR", "  A[起案] --> B{必要か?}", "  B -- はい --> C[移管準備会議]",
               "  classDef accent fill:#87ceeb,stroke:#555;", "  X([stadium]) --> Y[[sub]]"):
        assert _balance_brackets(ln) == ln           # 正しい行は不変(冪等)


def test_balance_quote_safe():
    assert _balance_brackets('  A["a ] b"] --> C') == '  A["a ] b"] --> C'   # 引用内は無視


def test_balance_skips_comment():
    assert _balance_brackets("  %% note [unclosed") == "  %% note [unclosed"


def test_normalize_repairs_flowchart_block():
    src = "```mermaid\nflowchart LR\n  E --> F[問題発覚?}\n  L --> M[手順書承\n```"
    out = normalize_mermaid(src)
    assert "F[問題発覚?]" in out and "M[手順書承]" in out


def test_normalize_leaves_non_flowchart_brackets():
    # sequenceDiagram は flowchart ではないので括弧補修の対象外(誤補修しない)
    src = "```mermaid\nsequenceDiagram\n  A->>B: msg [note}\n```"
    assert "[note}" in normalize_mermaid(src)


# ---------------- 推論/特殊トークン除去の拡張 ----------------
def test_strip_thinking_tag_variant():
    assert strip_think("<thinking>推論</thinking>本文") == "本文"
    assert strip_think("前<thinking>x</thinking>後") == "前後"


def test_strip_leaked_special_tokens():
    assert strip_think("答えです。<|im_end|>") == "答えです。"
    assert strip_think("回答<|eot_id|>") == "回答"


def test_strip_harmony_final_channel():
    s = ("<|start|>assistant<|channel|>analysis<|message|>考え中<|end|>"
         "<|start|>assistant<|channel|>final<|message|>これが答え<|return|>")
    assert strip_think(s).strip() == "これが答え"


def test_strip_harmony_analysis_only():
    s = "<|channel|>analysis<|message|>内部推論<|end|>表示する本文"
    assert strip_think(s).strip() == "表示する本文"


def test_strip_keeps_normal_angle_text():
    # 思考でも特殊トークンでもない < > は残す(本文を壊さない)
    assert strip_think("条件 a<b かつ c>d を満たす") == "条件 a<b かつ c>d を満たす"


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
