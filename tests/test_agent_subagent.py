"""app.agent の調査サブエージェント(_run_subagent / explore)の単体テスト。

Ollama クライアントはフェイクに差し替え(ネットワーク・実モデル不要)。読み取り専用の
小ループが「ツール実行→要約返却」「変更系の拒否」「空タスク」を正しく扱うか検証する。
"""
import contextlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agent import _impl   # noqa: E402


class _Fn:
    def __init__(self, name, args):
        self.name = name
        self.arguments = args


class _TC:
    def __init__(self, name, args):
        self.function = _Fn(name, args)


class _Msg:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class _Resp:
    def __init__(self, msg):
        self.message = msg


class _FakeClient:
    """scripted の順に message を返すフェイク。chat の引数は無視する。"""
    def __init__(self, scripted):
        self.scripted = scripted
        self.i = 0

    def chat(self, **kw):
        m = self.scripted[min(self.i, len(self.scripted) - 1)]
        self.i += 1
        return _Resp(m)


@contextlib.contextmanager
def fake(scripted):
    d = tempfile.mkdtemp(prefix="subagent_test_")
    old = _impl._client
    _impl._client = lambda *a, **k: _FakeClient(scripted)
    try:
        yield Path(d).resolve()
    finally:
        _impl._client = old
        shutil.rmtree(d, ignore_errors=True)


def test_subagent_returns_content_when_no_tools():
    with fake([_Msg(content="調査結果: 認証は auth.py にあります")]) as ws:
        out = _impl._run_subagent("m", ws, "認証を調べて", None)
        assert "auth.py" in out


def test_subagent_runs_readonly_tool_then_summarizes():
    scripted = [_Msg(tool_calls=[_TC("grep", {"pattern": "TARGET"})]),
                _Msg(content="見つかりました: f.txt")]
    with fake(scripted) as ws:
        (ws / "f.txt").write_text("TARGET here\n", encoding="utf-8")
        out = _impl._run_subagent("m", ws, "TARGET を探して", None)
        assert "見つかりました" in out


def test_subagent_blocks_write_tool():
    scripted = [_Msg(tool_calls=[_TC("write_file", {"path": "x.txt", "content": "y"})]),
                _Msg(content="完了")]
    with fake(scripted) as ws:
        out = _impl._run_subagent("m", ws, "何かして", None)
        assert out == "完了"
        assert not (ws / "x.txt").exists()   # 変更系は拒否され、ファイルは作られない


def test_subagent_empty_task():
    with fake([_Msg(content="x")]) as ws:
        assert _impl._run_subagent("m", ws, "   ", None).startswith("[エラー]")


# 調査結果が文脈を溢れさせる前に、要約して打ち切る(num_ctx 連動)
def _big_file_scripted():
    big = ("データ行 " * 12 + "\n") * 220        # 1万字超 → 取り込み時にキャップ
    scripted = [_Msg(tool_calls=[_TC("read_file", {"path": "big.txt"})]),
                _Msg(content="STOPPED", tool_calls=[_TC("grep", {"pattern": "データ"})]),
                _Msg(content="完了")]
    return big, scripted


def test_subagent_stops_early_when_context_full():
    big, scripted = _big_file_scripted()
    with fake(scripted) as ws:
        (ws / "big.txt").write_text(big, encoding="utf-8")
        out = _impl._run_subagent("m", ws, "big.txt を調べて", 8192)   # 小 num_ctx → 早期打ち切り
        assert out == "STOPPED"      # 次のツールへ進まず、その場の要約で返す


def test_subagent_continues_when_context_has_room():
    big, scripted = _big_file_scripted()
    with fake(scripted) as ws:
        (ws / "big.txt").write_text(big, encoding="utf-8")
        out = _impl._run_subagent("m", ws, "big.txt を調べて", None)    # 余裕あり → 続行
        assert out == "完了"


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
