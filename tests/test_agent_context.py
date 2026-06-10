"""エージェントの文脈圧縮(num_ctx 連動)と変更意図検出の単体テスト。

- _compact_threshold: num_ctx から圧縮しきい値を算出(不明時は固定値)
- compact_ctx: char_limit を超えたときだけ rest を要約1件へ畳み込む(head は保持)
- _CHANGE_INTENT: 「作って/書いて/削除」等の活用形でも変更意図を拾う(安全網の発火条件)
Ollama には接続しない(要約関数はフェイク)。pytest でも単体実行でも動く。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agent import _impl                       # noqa: E402
from app.agent.constants import CTX_CHAR_LIMIT, _CHANGE_INTENT   # noqa: E402
from app.agent.context import (                   # noqa: E402
    _ctx_chars, compact_ctx, force_truncate_ctx)


# ---------------- _compact_threshold ----------------
def test_compact_threshold_fallback_when_unknown():
    assert _impl._compact_threshold(0) == CTX_CHAR_LIMIT
    assert _impl._compact_threshold(None) == CTX_CHAR_LIMIT


def test_compact_threshold_scales_with_num_ctx():
    # 固定費(system+ツール定義 ~8千トークン)と日本語1文字≈1トークンを見込んだ安全側の式
    assert _impl._compact_threshold(8192) == 4000                        # 下限 4000
    assert _impl._compact_threshold(16384) == int((16384 - 6000) * 0.6)
    assert _impl._compact_threshold(32768) == int((32768 - 6000) * 0.6)
    assert _impl._compact_threshold(8192) < _impl._compact_threshold(32768)
    # 小さい窓でも、しきい値が窓そのものを超えない(溢れる前に必ず圧縮が起動する)
    assert _impl._compact_threshold(16384) < 16384


# ---------------- compact_ctx(char_limit 連動) ----------------
def _msgs():
    base = [{"role": "system", "content": "s"},
            {"role": "user", "content": "ws"},
            {"role": "assistant", "content": "ok"}]          # head(設定)= 3件
    base += [{"role": "user", "content": "x" * 100},
             {"role": "assistant", "content": "y" * 100}] * 3   # rest = 6件
    return base


def test_compact_ctx_skips_under_limit():
    assert compact_ctx(_msgs(), lambda t: "要約", char_limit=10_000) is False


def test_compact_ctx_folds_over_limit():
    m = _msgs()
    n0 = len(m)
    assert compact_ctx(m, lambda t: "要約", char_limit=100) is True
    assert len(m) < n0 and m[-1]["content"].endswith("要約")   # head 保持 + rest を要約1件へ


# ---------------- compact_ctx(要約LLMへ渡す transcript の上限) ----------------
def test_compact_ctx_caps_transcript_for_small_window():
    m = _msgs()
    seen = {}
    def summ(text):
        seen["len"] = len(text)
        return "要約"
    assert compact_ctx(m, summ, char_limit=100, transcript_cap=120) is True
    assert seen["len"] <= 1000   # 下限1000未満には絞らないが、40000固定では送らない


# ---------------- _ctx_chars(tool_calls の引数も算入) ----------------
def test_ctx_chars_counts_tool_call_arguments():
    plain = [{"role": "assistant", "content": "x"}]
    with_tc = [{"role": "assistant", "content": "x",
                "tool_calls": [{"function": {"name": "write_file",
                                             "arguments": {"path": "a", "content": "y" * 500}}}]}]
    assert _ctx_chars(with_tc) > _ctx_chars(plain) + 500


# ---------------- force_truncate_ctx(LLM不要の最終手段) ----------------
def test_force_truncate_cuts_long_tool_results():
    m = _msgs() + [{"role": "tool", "content": "z" * 9000, "tool_name": "read_file"}]
    assert force_truncate_ctx(m, char_limit=10_000) is True
    tool = next(x for x in m if x.get("role") == "tool")
    assert len(tool["content"]) < 2000 and "自動切り詰め" in tool["content"]


def test_force_truncate_drops_old_messages_keeps_head_and_tail():
    base = [{"role": "system", "content": "s"},
            {"role": "user", "content": "ws"},
            {"role": "assistant", "content": "ok"}]
    base += [{"role": "user", "content": f"u{i}" + "x" * 300} for i in range(30)]
    last = base[-1]["content"]
    assert force_truncate_ctx(base, char_limit=2000) is True
    assert base[0]["role"] == "system"                  # head は維持
    assert base[-1]["content"] == last                  # 直近は維持
    assert any("省略しました" in x.get("content", "") for x in base)   # 間引きの注記
    assert _ctx_chars(base) <= 2000 + 500               # 注記ぶんの余裕を見て上限付近まで縮む


def test_force_truncate_noop_when_small():
    m = _msgs()
    before = [dict(x) for x in m]
    assert force_truncate_ctx(m, char_limit=100_000) is False
    assert m == before


# ---------------- _CHANGE_INTENT(活用形) ----------------
def test_change_intent_matches_conjugations():
    for s in ("READMEを作って", "テストを書いて", "この関数を直して",
              "古いファイルを削除して", "設定を変更してほしい", "create a test", "remove the file"):
        assert _CHANGE_INTENT.search(s), s


def test_change_intent_ignores_plain_questions():
    for s in ("有給は何日もらえますか?", "この関数の意味は?", "どこに置いてある?"):
        assert not _CHANGE_INTENT.search(s), s


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
