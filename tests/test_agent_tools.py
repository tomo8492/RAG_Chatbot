"""app.agent のファイル操作ツール・パス安全・undo・自動構文チェックの単体テスト。

Ollama 等の外部サービスには一切接続しない(一時フォルダ上の純粋なファイル操作と
ロジックのみを検証する)。pytest でも、`python tests/test_agent_tools.py` 単体実行
でも動く。
"""
import contextlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import agent  # noqa: E402
from app.agent import _impl  # noqa: E402


@contextlib.contextmanager
def workspace():
    """毎テスト独立の一時作業フォルダ。終了時に後始末し undo 台帳もクリアする。"""
    d = tempfile.mkdtemp(prefix="agent_test_")
    try:
        yield Path(d).resolve()
    finally:
        shutil.rmtree(d, ignore_errors=True)
        agent._UNDO.clear()


# ---------------- write / read ----------------
def test_write_then_read_roundtrip():
    with workspace() as ws:
        assert agent.t_write_file(ws, "a.txt", "こんにちは\n世界").startswith("[OK]")
        assert agent.t_read_file(ws, "a.txt").startswith("こんにちは")


def test_write_reports_overwrite():
    with workspace() as ws:
        agent.t_write_file(ws, "a.txt", "1")
        assert "上書き" in agent.t_write_file(ws, "a.txt", "2")


def test_read_missing_file():
    with workspace() as ws:
        assert agent.t_read_file(ws, "nope.txt").startswith("[エラー]")


def test_read_offset_limit_window():
    with workspace() as ws:
        agent.t_write_file(ws, "n.txt", "\n".join(str(i) for i in range(1, 11)))
        out = agent.t_read_file(ws, "n.txt", offset=3, limit=2)
        assert out.splitlines()[0] == "3"          # offset は1始まり
        assert "続きは offset=" in out


def test_read_offset_out_of_range():
    with workspace() as ws:
        agent.t_write_file(ws, "n.txt", "a\nb")
        assert agent.t_read_file(ws, "n.txt", offset=99).startswith("[エラー]")


# ---------------- edit ----------------
def test_edit_unique():
    with workspace() as ws:
        agent.t_write_file(ws, "f.txt", "alpha beta gamma")
        assert agent.t_edit_file(ws, "f.txt", "beta", "BETA").startswith("[OK]")
        assert agent.t_read_file(ws, "f.txt") == "alpha BETA gamma"


def test_edit_ambiguous_needs_replace_all():
    with workspace() as ws:
        agent.t_write_file(ws, "f.txt", "x x x")
        r = agent.t_edit_file(ws, "f.txt", "x", "y")
        assert r.startswith("[エラー]") and "replace_all" in r


def test_edit_replace_all():
    with workspace() as ws:
        agent.t_write_file(ws, "f.txt", "x x x")
        assert agent.t_edit_file(ws, "f.txt", "x", "y", replace_all=True).startswith("[OK]")
        assert agent.t_read_file(ws, "f.txt") == "y y y"


def test_edit_not_found():
    with workspace() as ws:
        agent.t_write_file(ws, "f.txt", "abc")
        assert agent.t_edit_file(ws, "f.txt", "zzz", "q").startswith("[エラー]")


def test_edit_same_string():
    with workspace() as ws:
        agent.t_write_file(ws, "f.txt", "abc")
        assert agent.t_edit_file(ws, "f.txt", "abc", "abc").startswith("[エラー]")


# ---------------- パス安全(作業フォルダの外には出さない) ----------------
def test_safe_path_blocks_parent_escape():
    with workspace() as ws:
        try:
            agent._safe_path(ws, "../escape.txt")
        except ValueError:
            return
        raise AssertionError("親フォルダ脱出が許可された")


def test_write_outside_workspace_blocked():
    with workspace() as ws:
        assert agent.t_write_file(ws, "../evil.txt", "x").startswith("[エラー]")


def test_read_outside_workspace_blocked():
    with workspace() as ws:
        assert agent.t_read_file(ws, "../../etc/passwd").startswith("[エラー]")


# ---------------- glob / grep / list ----------------
def test_glob_matches_pattern():
    with workspace() as ws:
        agent.t_write_file(ws, "a.py", "1")
        agent.t_write_file(ws, "sub/b.py", "2")
        agent.t_write_file(ws, "c.txt", "3")
        out = agent.t_glob(ws, "**/*.py").splitlines()
        assert "a.py" in out and "sub/b.py" in out and "c.txt" not in out


def test_glob_ignores_vcs_dirs():
    with workspace() as ws:
        agent.t_write_file(ws, ".git/config", "x")
        agent.t_write_file(ws, "keep.py", "x")
        out = agent.t_glob(ws, "**/*")
        assert "keep.py" in out and ".git" not in out


def test_grep_finds_line():
    with workspace() as ws:
        agent.t_write_file(ws, "f.txt", "one\nTARGET here\nthree")
        assert "f.txt:2:" in agent.t_grep(ws, "TARGET")


def test_grep_invalid_regex():
    with workspace() as ws:
        assert agent.t_grep(ws, "(unclosed").startswith("[エラー]")


def test_list_files_excludes_ignored():
    with workspace() as ws:
        agent.t_write_file(ws, "keep.txt", "x")
        agent.t_write_file(ws, "node_modules/dep.js", "x")
        out = agent.t_list_files(ws)
        assert "keep.txt" in out and "node_modules" not in out


# ---------------- 自動構文チェック(自己検証) ----------------
def test_syntax_check_valid_python():
    with workspace() as ws:
        agent.t_write_file(ws, "ok.py", "x = 1\n")
        assert agent._syntax_check(ws, "ok.py") is None


def test_syntax_check_invalid_python():
    with workspace() as ws:
        agent.t_write_file(ws, "bad.py", "def f(:\n")
        assert agent._syntax_check(ws, "bad.py") is not None


def test_syntax_check_unknown_ext_skipped():
    with workspace() as ws:
        agent.t_write_file(ws, "x.txt", "def f(:")
        assert agent._syntax_check(ws, "x.txt") is None


# ---------------- 変更の適用 + 取り消し(undo) ----------------
def test_apply_change_new_file_undo_deletes():
    with workspace() as ws:
        _, ev = agent._apply_change(ws, "write_file", {"path": "new.txt", "content": "hi"}, {})
        assert ev.get("undo_id")
        assert (ws / "new.txt").is_file()
        assert "削除" in agent.undo(ev["undo_id"])
        assert not (ws / "new.txt").exists()


def test_apply_change_edit_undo_restores():
    with workspace() as ws:
        agent.t_write_file(ws, "f.txt", "original")
        _, ev = agent._apply_change(
            ws, "edit_file",
            {"path": "f.txt", "old_string": "original", "new_string": "changed"}, {})
        assert agent.t_read_file(ws, "f.txt") == "changed"
        agent.undo(ev["undo_id"])
        assert agent.t_read_file(ws, "f.txt") == "original"


def test_apply_change_syntax_error_marks_error():
    with workspace() as ws:
        result, ev = agent._apply_change(
            ws, "write_file", {"path": "bad.py", "content": "def f(:\n"}, {})
        assert ev["status"] == "error"
        assert "[構文エラー]" in result


def test_undo_twice_fails():
    with workspace() as ws:
        _, ev = agent._apply_change(ws, "write_file", {"path": "n.txt", "content": "x"}, {})
        agent.undo(ev["undo_id"])
        assert agent.undo(ev["undo_id"]).startswith("[エラー]")


# ---------------- remember(CLAUDE.md への学習メモ) ----------------
def test_remember_creates_claude_md():
    with workspace() as ws:
        agent.t_remember(ws, "タブではなくスペースを使う")
        text = (ws / "CLAUDE.md").read_text(encoding="utf-8")
        assert "メモ" in text and "タブではなくスペースを使う" in text


def test_remember_dedup():
    with workspace() as ws:
        agent.t_remember(ws, "同じメモ")
        assert "既に記録済み" in agent.t_remember(ws, "同じメモ")


def test_remember_empty():
    with workspace() as ws:
        assert agent.t_remember(ws, "   ").startswith("[エラー]")


# ---------------- ステータス判定 ----------------
def test_result_status():
    assert agent._result_status("[エラー] x") == "error"
    assert agent._result_status("[OK] done") == "ok"
    assert agent._result_status("") == "ok"


# ---------------- read前提の強制ゲート(盲目編集の防止) ----------------
def test_require_read_first_blocks_unread_edit():
    with workspace() as ws:
        agent.t_write_file(ws, "f.py", "x = 1\n")
        msg = _impl._require_read_first(ws, "edit_file", {"path": "f.py"}, set())
        assert msg and "read_file" in msg


def test_require_read_first_allows_after_read():
    with workspace() as ws:
        agent.t_write_file(ws, "f.py", "x = 1\n")
        seen: set = set()
        _impl._mark_read(ws, "f.py", seen)
        assert _impl._require_read_first(ws, "edit_file", {"path": "f.py"}, seen) is None


def test_require_read_first_allows_new_file_write():
    with workspace() as ws:                       # 新規(未存在)への write_file は read 不要
        assert _impl._require_read_first(ws, "write_file", {"path": "new.py"}, set()) is None


def test_require_read_first_blocks_unread_overwrite():
    with workspace() as ws:                       # 既存への全文上書きは read を要求
        agent.t_write_file(ws, "f.py", "x = 1\n")
        msg = _impl._require_read_first(ws, "write_file", {"path": "f.py"}, set())
        assert msg and "read_file" in msg


def test_require_read_first_empty_path():
    with workspace() as ws:
        assert _impl._require_read_first(ws, "edit_file", {"path": ""}, set()) is None


def test_edit_not_found_suggests_read():
    with workspace() as ws:
        agent.t_write_file(ws, "f.txt", "abc")
        assert "read_file" in agent.t_edit_file(ws, "f.txt", "zzz", "q")


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
