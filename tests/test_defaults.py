"""app.defaults.effective_for の num_ctx 解決の単体テスト。

num_ctx=0(自動)が、溢れやすいモデル既定ではなく安全な既定(settings.num_ctx)へ
解決されること、明示値はそのまま保たれることを検証する(get_defaults をスタブ=DB不要)。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import defaults                # noqa: E402
from app.config import settings         # noqa: E402


def _with_defaults(d, fn):
    old = defaults.get_defaults
    defaults.get_defaults = lambda: dict(d)
    try:
        return fn()
    finally:
        defaults.get_defaults = old


def test_effective_for_resolves_auto_num_ctx():
    eff = _with_defaults({"num_ctx": 0}, lambda: defaults.effective_for({}))
    assert settings.num_ctx > 0
    assert eff["num_ctx"] == settings.num_ctx     # 0(自動)→ 安全な既定へ


def test_effective_for_keeps_explicit_num_ctx():
    eff = _with_defaults({"num_ctx": 16384}, lambda: defaults.effective_for({}))
    assert eff["num_ctx"] == 16384                # 明示値はそのまま


def test_effective_for_conv_override_then_resolve():
    # 会話側で 0 を指定 → 自動扱いで安全な既定へ解決
    eff = _with_defaults({"num_ctx": 8192},
                         lambda: defaults.effective_for({"settings": {"num_ctx": 0}}))
    assert eff["num_ctx"] == settings.num_ctx


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
