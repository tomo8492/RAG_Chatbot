"""app.agent.verify(自律検証ループの部品)の単体テスト。

外部サービスへは接続しない。検証コマンドの自動検出と、実行 (run_verify) の
成功/失敗判定を一時フォルダ上で検証する。pytest でも単体実行でも動く。
"""
import contextlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agent import verify  # noqa: E402


@contextlib.contextmanager
def workspace():
    d = tempfile.mkdtemp(prefix="verify_test_")
    try:
        yield Path(d).resolve()
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------------- detect_verify_cmd ----------------
def test_detect_python_pytest():
    with workspace() as ws:
        (ws / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        assert verify.detect_verify_cmd(ws) == "pytest -q"


def test_detect_python_by_tests_dir():
    with workspace() as ws:
        (ws / "tests").mkdir()
        assert verify.detect_verify_cmd(ws) == "pytest -q"


def test_detect_node_test_script():
    with workspace() as ws:
        (ws / "package.json").write_text('{"scripts": {"test": "jest"}}', encoding="utf-8")
        assert verify.detect_verify_cmd(ws) == "npm test --silent"


def test_detect_node_build_when_no_test():
    with workspace() as ws:
        (ws / "package.json").write_text('{"scripts": {"build": "vite build"}}', encoding="utf-8")
        assert verify.detect_verify_cmd(ws) == "npm run build"


def test_detect_none_for_plain_folder():
    with workspace() as ws:
        (ws / "readme.txt").write_text("hello", encoding="utf-8")
        assert verify.detect_verify_cmd(ws) == ""


# ---------------- run_verify ----------------
def test_run_verify_empty_command():
    with workspace() as ws:
        ok, out = verify.run_verify(ws, "")
        assert ok is False and out.startswith("[エラー]")


def test_run_verify_success():
    with workspace() as ws:
        ok, out = verify.run_verify(ws, f'"{sys.executable}" -c "pass"')
        assert ok is True and "[検証OK]" in out


def test_run_verify_failure_reports_exit_code():
    with workspace() as ws:
        ok, out = verify.run_verify(ws, f'"{sys.executable}" -c "import sys; sys.exit(3)"')
        assert ok is False and "終了コード 3" in out


# ---------------- resolve_verify_cmds / run_checks(検証手段の自由化・複数チェック)----------------
def test_resolve_verify_cmds_uses_setting_multiline():
    with workspace() as ws:
        cmds = verify.resolve_verify_cmds(ws, "pytest -q\n ruff check \n")
        assert cmds == ["pytest -q", "ruff check"]   # 設定優先・改行で複数・空行は除去


def test_resolve_verify_cmds_autodetects_when_empty():
    with workspace() as ws:
        (ws / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        assert verify.resolve_verify_cmds(ws, "") == ["pytest -q"]


def test_resolve_verify_cmds_empty_when_nothing():
    with workspace() as ws:
        assert verify.resolve_verify_cmds(ws, "   ") == []


def test_run_checks_all_pass():
    with workspace() as ws:
        ok, out = verify.run_checks(ws, [f'"{sys.executable}" -c "pass"'])
        assert ok is True and "[検証OK]" in out


def test_run_checks_one_fails_overall_fail():
    with workspace() as ws:
        ok, out = verify.run_checks(ws, [f'"{sys.executable}" -c "pass"',
                                         f'"{sys.executable}" -c "import sys; sys.exit(1)"'])
        assert ok is False and "終了コード 1" in out


def test_run_checks_empty():
    with workspace() as ws:
        ok, out = verify.run_checks(ws, [])
        assert ok is False and out.startswith("[エラー]")


# ---------------- detect_verify_cmds(lint/型の自動追加) ----------------
def test_detect_adds_ruff_and_mypy_when_configured():
    with workspace() as ws:
        (ws / "pyproject.toml").write_text(
            "[tool.ruff]\nline-length=100\n[tool.mypy]\nstrict=true\n", encoding="utf-8")
        cmds = verify.detect_verify_cmds(ws)
        assert "pytest -q" in cmds and "ruff check ." in cmds and "mypy ." in cmds


def test_detect_skips_lint_without_config():
    with workspace() as ws:
        (ws / "tests").mkdir()                      # pytest は出るが ruff/mypy 設定なし
        cmds = verify.detect_verify_cmds(ws)
        assert cmds == ["pytest -q"]                # 無関係な lint を足さない


def test_detect_node_lint_and_typecheck():
    import json as _j
    with workspace() as ws:
        (ws / "package.json").write_text(_j.dumps(
            {"scripts": {"test": "jest", "lint": "eslint .", "typecheck": "tsc --noEmit"}}),
            encoding="utf-8")
        cmds = verify.detect_verify_cmds(ws)
        assert "npm test --silent" in cmds and "npm run lint --silent" in cmds \
            and "npm run typecheck --silent" in cmds


def test_detect_tsc_from_tsconfig():
    import json as _j
    with workspace() as ws:
        (ws / "package.json").write_text(_j.dumps({"scripts": {"build": "vite build"}}),
                                         encoding="utf-8")
        (ws / "tsconfig.json").write_text("{}", encoding="utf-8")
        cmds = verify.detect_verify_cmds(ws)
        assert any("tsc --noEmit" in c for c in cmds)


def test_missing_tool_is_skipped_not_failed():
    with workspace() as ws:
        ok, out = verify.run_verify(ws, "definitely_not_a_real_tool_xyz --check")
        assert ok is True and "スキップ" in out           # 未導入は失敗にしない


def test_looks_missing_tool_detector():
    assert verify._looks_missing_tool(127, "bash: foo: command not found")
    assert verify._looks_missing_tool(1, "'tsc' is not recognized as an internal command")
    assert not verify._looks_missing_tool(1, "AssertionError: test failed")


# ---------------- 生成パラメータの上書き(_gen_options) ----------------
def test_gen_options_defaults_and_override():
    from app.agent import _impl
    d = _impl._gen_options(16384)
    assert d["temperature"] == _impl.AGENT_TEMPERATURE and d["num_ctx"] == 16384
    o = _impl._gen_options(8192, {"temperature": 0.05, "top_p": 0.5, "num_predict": 4096})
    assert o["temperature"] == 0.05 and o["top_p"] == 0.5 and o["num_predict"] == 4096
    # 範囲外はクランプ、不正値は既定へフォールバック
    c = _impl._gen_options(None, {"temperature": 9.9, "num_predict": "x"})
    assert c["temperature"] == 2.0 and c["num_predict"] == _impl.AGENT_NUM_PREDICT


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
