"""CodeAgent レビューで見つけた問題の回帰テスト。

  - 拒否/ブロックされた変更を「適用済み(読了・要検証)」と誤認しない
  - _apply_change の applied フラグ(構文エラーでも書き込み自体は適用)
  - undo 台帳・終了済みバックグラウンドjob・承認待ちエントリの上限/掃除
  - explore(サブエージェント調査)が ask_user ガードの「調査済み」に数えられる

Ollama には接続しない(クライアントとサブエージェントはフェイクに差し替える)。
pytest でも `python tests/test_agent_review.py` でも動く。
"""
import contextlib
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import agent                    # noqa: E402
from app.agent import _impl, approvals   # noqa: E402
from app.agent import tools as agtools   # noqa: E402


@contextlib.contextmanager
def workspace():
    d = tempfile.mkdtemp(prefix="agent_review_")
    try:
        yield Path(d).resolve()
    finally:
        shutil.rmtree(d, ignore_errors=True)
        agent._UNDO.clear()


@contextlib.contextmanager
def patched(obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


def _chunk(content=None, tool_calls=None):
    return SimpleNamespace(message=SimpleNamespace(
        thinking=None, content=content, tool_calls=tool_calls or []))


def _tc(name, args):
    return SimpleNamespace(function=SimpleNamespace(name=name, arguments=args))


class FakeClient:
    """run_stream 用のフェイク Ollama クライアント。turns の各要素=1回の chat 応答。
    要素が Exception ならその呼び出しで送出する(コンテキスト超過の再現用)。"""

    def __init__(self, turns):
        self.turns = list(turns)

    def chat(self, model=None, messages=None, tools=None, stream=False, options=None):
        assert self.turns, "想定外の追加 chat 呼び出し"
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        if stream:
            return iter(turn)
        return turn[-1]


def _run(ws, turns, decision=(True, None, None), answer=None, subagent=None,
         messages=None, plan_mode=False, plan_ok=True, **kw):
    """run_stream をフェイク環境で最後まで回し、イベント一覧を返す。"""
    events = []
    client = FakeClient(turns)
    msgs = messages if messages is not None else [{"role": "user", "content": "修正して"}]
    with patched(_impl, "_client", lambda: client), \
         patched(_impl, "wait", lambda aid, timeout=1: plan_ok), \
         patched(_impl, "wait_decision", lambda aid, timeout=1: decision), \
         patched(_impl, "wait_answer", lambda aid, timeout=1: answer), \
         patched(_impl, "_run_subagent", subagent or (lambda *a, **k: "(調査なし)")):
        for ev in _impl.run_stream("fake-model", msgs, str(ws),
                                   allow_changes=True, plan_mode=plan_mode, **kw):
            events.append(ev)
    return events


# ---------------- _apply_change の applied フラグ ----------------
def test_apply_change_reports_applied_flag():
    with workspace() as ws:
        _, ev = _impl._apply_change(ws, "write_file", {"path": "a.txt", "content": "x"}, {})
        assert ev["applied"] is True and ev["status"] == "ok"


def test_apply_change_failed_edit_not_applied():
    with workspace() as ws:
        (ws / "a.txt").write_text("hello", encoding="utf-8")
        _, ev = _impl._apply_change(
            ws, "edit_file", {"path": "a.txt", "old_string": "zzz", "new_string": "y"}, {})
        assert ev["applied"] is False and ev["status"] == "error"


def test_apply_change_syntax_error_still_applied():
    with workspace() as ws:
        _, ev = _impl._apply_change(ws, "write_file", {"path": "b.py", "content": "def f(:\n"}, {})
        # 書き込み自体は成功(=applied)だが、構文エラーとして差し戻す
        assert ev["applied"] is True and ev["status"] == "error"
        assert (ws / "b.py").is_file()


def test_undo_ledger_capped():
    with workspace() as ws:
        for i in range(_impl._UNDO_MAX + 7):
            _impl._apply_change(ws, "write_file", {"path": f"f{i}.txt", "content": "x"}, {})
        assert len(agent._UNDO) == _impl._UNDO_MAX


# ---------------- 拒否された変更は「適用済み」扱いにしない ----------------
def test_rejected_change_not_marked_applied():
    with workspace() as ws:
        turns = [
            [_chunk(tool_calls=[_tc("write_file", {"path": "b.py", "content": "x = 1\n"})])],
            [_chunk(content="了解しました")],
        ]
        events = _run(ws, turns, decision=(False, None, "やめて"),
                      auto_verify=True, verify_cmd="echo ok")
        assert not (ws / "b.py").exists()   # 拒否されたのでファイルは作られない
    assert any(e.get("type") == "tool_result" and e.get("name") == "write_file"
               and e.get("status") == "rejected" for e in events)
    # 変更は適用されていないので、自動検証(verify)は走らない
    assert not any(e.get("name") == "verify" for e in events)
    assert events[-1]["type"] == "done"
    assert all("applied" not in e for e in events)   # 内部フラグはイベントに漏らさない


def test_approved_change_marks_applied_and_verifies():
    with workspace() as ws:
        turns = [
            [_chunk(tool_calls=[_tc("write_file", {"path": "b.py", "content": "x = 1\n"})])],
            [_chunk(content="完了")],
        ]
        events = _run(ws, turns, decision=(True, None, None),
                      auto_verify=True, verify_cmd="echo ok")
        assert (ws / "b.py").read_text(encoding="utf-8") == "x = 1\n"
    applied = next(e for e in events if e.get("type") == "tool_result"
                   and e.get("name") == "write_file")
    assert applied["status"] == "ok" and applied.get("undo_id")
    # 適用されたので自動検証が1回走り、成功して終了する
    verifies = [e for e in events if e.get("name") == "verify"]
    assert any(e.get("type") == "tool_call" for e in verifies)
    assert any(e.get("type") == "tool_result" and e.get("status") == "ok" for e in verifies)
    assert events[-1]["type"] == "done"


# ---------------- explore は「調査済み」に数える(ask_user ガード)----------------
def test_explore_counts_as_investigation_for_ask_user():
    with workspace() as ws:
        ask_args = {"questions": [{"question": "どちらにしますか?", "header": "方式",
                                   "options": [{"label": "A"}, {"label": "B"}]}]}
        turns = [
            [_chunk(tool_calls=[_tc("explore", {"task": "構成を調べて"})])],
            [_chunk(tool_calls=[_tc("ask_user", ask_args)])],
            [_chunk(content="Aで進めます")],
        ]
        events = _run(ws, turns, answer=[["A"]],
                      subagent=lambda *a, **k: "調査結果: モジュールは2つ")
    # explore 後の ask_user はリダイレクトされず、質問カードが出る
    assert any(e.get("type") == "ask" for e in events)
    assert not any(e.get("status") == "redirected" for e in events)
    ask_res = next(e for e in events if e.get("type") == "tool_result"
                   and e.get("name") == "ask_user")
    assert "A" in ask_res["result"]


# ---------------- コンテキスト超過(400)からの自己回復 ----------------
_OVERFLOW = ('{"error":{"code":400,"message":"request (10879 tokens) exceeds '
             'the available context size (8192 tokens)"}} (status code: 400)')


def test_ctx_overflow_truncates_and_retries():
    with workspace() as ws:
        # 実際の文脈構造(system+設定+ack の head つき)で、巨大なツール結果による超過を再現
        big = [{"role": "system", "content": "s"},
               {"role": "user", "content": "ws"},
               {"role": "assistant", "content": "了解しました。依頼をどうぞ。"},
               {"role": "user", "content": "調べて"},
               {"role": "tool", "content": "x" * 50_000, "tool_name": "read_file"}]
        turns = [
            RuntimeError(_OVERFLOW),    # ストリーミング → 400
            RuntimeError(_OVERFLOW),    # 非ストリーム フォールバック → 400
            [_chunk(content="回復しました")],   # 強制切り詰め後の再試行 → 成功
        ]
        events = _run(ws, turns, messages=big, num_ctx=8192)
        assert not any(e.get("type") == "error" for e in events)
        assert events[-1]["type"] == "done"
        tool = next(m for m in big if m.get("role") == "tool")
        assert len(tool["content"]) < 3000   # ツール結果が実際に切り詰められている


def test_ctx_overflow_final_error_is_actionable():
    with workspace() as ws:
        turns = [RuntimeError(_OVERFLOW), RuntimeError(_OVERFLOW), RuntimeError(_OVERFLOW)]
        events = _run(ws, turns, messages=[{"role": "user", "content": "x"}], num_ctx=8192)
    err = next(e for e in events if e.get("type") == "error")
    assert "コンテキスト長" in err["error"] and "16384" in err["error"]   # 生JSONではなく対処を案内
    assert "num_ctx=8192" in err["error"]


def test_is_ctx_overflow_detector():
    assert _impl._is_ctx_overflow(_OVERFLOW)
    assert _impl._is_ctx_overflow("exceed_context_size_error")
    assert not _impl._is_ctx_overflow("connection refused")


# ---------------- present_plan の引数ゆらぎ(配列で来てもクラッシュしない)----------------
def test_present_plan_accepts_list_argument():
    with workspace() as ws:
        turns = [
            [_chunk(tool_calls=[_tc("present_plan", {"plan": ["手順A", "手順B"]})])],
            [_chunk(content="完了")],
        ]
        events = _run(ws, turns, plan_mode=True, plan_ok=True)
    plan = next(e for e in events if e.get("type") == "plan")
    assert plan["plan"] == "1. 手順A\n2. 手順B"   # 配列 → 番号付き手順に正規化
    assert events[-1]["type"] == "done"


# ---------------- 復元ツールJSONの本文除去(assistant_clean) ----------------
def test_text_tool_recovery_emits_cleaned_content():
    with workspace() as ws:
        body = '調べます。\n```json\n{"name": "list_files", "arguments": {}}\n```\n以上。'
        turns = [
            [_chunk(content=body)],
            [_chunk(content="空でした")],
        ]
        events = _run(ws, turns)
    clean = next(e for e in events if e.get("type") == "assistant_clean")
    assert "調べます" in clean["text"] and "{" not in clean["text"]   # JSONと空フェンスを除去
    assert any(e.get("type") == "tool_result" and e.get("name") == "list_files"
               for e in events)


# ---------------- 自アプリの .env 保護 ----------------
def test_app_env_is_protected_file():
    from app import safety
    from app.config import ROOT_DIR
    assert safety.is_protected_file(ROOT_DIR / ".env") is True
    assert safety.is_within_protected(ROOT_DIR / ".env") is True
    assert safety.is_protected_file(ROOT_DIR / "README.md") is False


def test_app_env_blocked_from_read_and_listing():
    from app.config import ROOT_DIR
    out = agent.t_read_file(ROOT_DIR, ".env")
    assert out.startswith("[エラー]") and "保護" in out
    assert agtools._rel_ok(ROOT_DIR, ROOT_DIR / ".env") is False
    assert agtools._rel_ok(ROOT_DIR, ROOT_DIR / "README.md") is True


def test_other_projects_env_still_editable():
    with workspace() as ws:   # 作業フォルダ内の(他プロジェクトの).env は通常どおり扱える
        assert agent.t_write_file(ws, ".env", "FOO=1").startswith("[OK]")
        assert agent.t_read_file(ws, ".env").startswith("FOO=1")


# ---------------- edit_file: old_string 空のときの誘導 ----------------
def test_edit_file_empty_old_string_suggests_write_file():
    with workspace() as ws:
        (ws / "a.txt").write_text("x", encoding="utf-8")
        out = agent.t_edit_file(ws, "a.txt", "", "y")
        assert out.startswith("[エラー]") and "write_file" in out


# ---------------- 承認待ちエントリの掃除(切断リーク防止)----------------
def test_stale_pending_pruned():
    aid_old = approvals.new_pending()
    with approvals._pending_lock:
        approvals._pending[aid_old]["created"] -= (approvals._STALE_AFTER + 1)
    aid_new = approvals.new_pending()
    try:
        with approvals._pending_lock:
            assert aid_old not in approvals._pending   # 古い取り残しは掃除される
            assert aid_new in approvals._pending       # 新しいものは残る
    finally:
        with approvals._pending_lock:
            approvals._pending.pop(aid_new, None)


def test_fresh_pending_survives_prune():
    aid1 = approvals.new_pending()
    aid2 = approvals.new_pending()
    try:
        with approvals._pending_lock:
            assert aid1 in approvals._pending and aid2 in approvals._pending
    finally:
        with approvals._pending_lock:
            approvals._pending.pop(aid1, None)
            approvals._pending.pop(aid2, None)


# ---------------- 終了済みバックグラウンドjobの間引き ----------------
def test_prune_finished_jobs_keeps_recent_and_running():
    with agtools._bg_lock:
        saved = dict(agtools._bg_jobs)
        agtools._bg_jobs.clear()
        try:
            for i in range(agtools.KEEP_FINISHED_JOBS + 5):
                agtools._bg_jobs[f"fin{i}"] = {"command": "x", "output": "", "returncode": 0,
                                               "running": False, "proc": None}
            agtools._bg_jobs["run1"] = {"command": "y", "output": "", "returncode": None,
                                        "running": True, "proc": None}
            agtools._prune_finished_jobs()
            done = [k for k, j in agtools._bg_jobs.items() if not j["running"]]
            assert len(done) == agtools.KEEP_FINISHED_JOBS
            assert "fin0" not in agtools._bg_jobs          # 最古から破棄
            assert f"fin{agtools.KEEP_FINISHED_JOBS + 4}" in agtools._bg_jobs
            assert "run1" in agtools._bg_jobs              # 実行中は残す
        finally:
            agtools._bg_jobs.clear()
            agtools._bg_jobs.update(saved)


# 承認の Event がスレッドをまたいで動くこと(基本動作の確認)
def test_resolve_wakes_waiter():
    aid = approvals.new_pending()
    got = {}

    def waiter():
        got["d"] = approvals.wait_decision(aid, timeout=5)

    th = threading.Thread(target=waiter)
    th.start()
    approvals.resolve(aid, True, scope="always", reason=None)
    th.join(timeout=5)
    assert got.get("d") == (True, "always", None)


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
