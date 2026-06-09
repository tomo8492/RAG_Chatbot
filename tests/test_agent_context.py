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
from app.agent.context import compact_ctx         # noqa: E402


# ---------------- _compact_threshold ----------------
def test_compact_threshold_fallback_when_unknown():
    assert _impl._compact_threshold(0) == CTX_CHAR_LIMIT
    assert _impl._compact_threshold(None) == CTX_CHAR_LIMIT


def test_compact_threshold_scales_with_num_ctx():
    assert _impl._compact_threshold(8192) == max(6000, 8192 - 5000)     # 下限 6000
    assert _impl._compact_threshold(32768) == 32768 - 5000
    assert _impl._compact_threshold(8192) < _impl._compact_threshold(32768)


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
