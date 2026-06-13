"""Code エージェントの実HTTP E2E(/api/conversations/{cid}/agent)。

TestClient で実アプリを起動し、auto-accept(自動適用)で write_file が
HTTPルート経由で適用される一連(SSE → 適用 → DB保存 → undo HTTP → 文脈の永続化/復元)
を検証する。承認の往復は run_stream レベルで test_agent_review が担保しているため、
ここでは TestClient のストリーミング+並行承認デッドロックを避けて auto-accept で通す。

Ollama はフェイク、ChromaDB/SQLite は一時環境の実体。pytest/単体実行両対応。
"""
import contextlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, llm                     # noqa: E402
from app import main as appmain             # noqa: E402
from app.agent import _impl                 # noqa: E402
from app.config import settings             # noqa: E402


@contextlib.contextmanager
def patched(obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


@contextlib.contextmanager
def temp_env():
    d = Path(tempfile.mkdtemp(prefix="agent_http_")).resolve()
    (d / "data").mkdir()
    ws = d / "work"
    ws.mkdir()
    old = (settings.data_dir, settings.db_path, settings.lan_only)
    settings.data_dir = d / "data"
    settings.db_path = d / "data" / "t.db"
    settings.lan_only = False
    try:
        db.init_db()
        yield ws
    finally:
        settings.data_dir, settings.db_path, settings.lan_only = old
        appmain._code_ctx.clear()
        _impl._UNDO.clear()
        shutil.rmtree(d, ignore_errors=True)


def _chunk(content=None, tool_calls=None):
    return SimpleNamespace(message=SimpleNamespace(
        thinking=None, content=content, tool_calls=tool_calls or []))


def _tc(name, args):
    return SimpleNamespace(function=SimpleNamespace(name=name, arguments=args))


class FakeClient:
    def __init__(self, turns):
        self.turns = list(turns)

    def chat(self, model=None, messages=None, tools=None, stream=False, options=None):
        turn = self.turns.pop(0) if self.turns else [_chunk(content="完了")]
        return iter(turn) if stream else turn[-1]


def _sse_events(resp_text: str) -> list:
    out = []
    for line in resp_text.splitlines():
        if line.startswith("data: "):
            try:
                out.append(json.loads(line[6:]))
            except Exception:
                pass
    return out


def _make_code_conv(client, ws, **settings_override):
    conv = client.post("/api/conversations", json={"kind": "code"}).json()
    cid = conv["id"]
    s = {"workspace": str(ws), "plan_mode": False, "allow_changes": True,
         "auto_accept_edits": True, "auto_verify": False}
    s.update(settings_override)
    client.patch(f"/api/conversations/{cid}", json={"settings": s})
    return cid


def test_agent_http_autoaccept_apply_undo_persist():
    from fastapi.testclient import TestClient
    with temp_env() as ws:
        turns = [
            [_chunk(tool_calls=[_tc("write_file", {"path": "hello.py", "content": "print(1)\n"})])],
            [_chunk(content="作成しました")],
        ]
        client = TestClient(appmain.app)
        with patched(_impl, "_client", lambda: FakeClient(turns)), \
             patched(llm, "is_ollama_available", lambda: True), \
             patched(appmain, "_make_title", lambda c, m: ""):
            cid = _make_code_conv(client, ws)
            r = client.post(f"/api/conversations/{cid}/agent",
                            json={"content": "hello.py を作って"})
            assert r.status_code == 200
            events = _sse_events(r.text)
            tr = [e for e in events if e.get("type") == "tool_result" and e.get("name") == "write_file"]
            assert tr and tr[0]["status"] == "ok", events
            assert (ws / "hello.py").read_text(encoding="utf-8") == "print(1)\n"
            undo_id = tr[0].get("undo_id")
            assert undo_id

            # undo の実HTTP往復 → 新規作成の取り消しでファイルが消える
            ru = client.post("/api/code/undo", json={"undo_id": undo_id}).json()
            assert ru["ok"] and not (ws / "hello.py").exists()

            # 文脈が KV に永続化(ツール往復を含む・画像base64は除去)
            saved = db.get_kv(f"code_ctx:{cid}")
            assert isinstance(saved, list)
            assert any(m.get("role") == "tool" for m in saved if isinstance(m, dict))
            assert all("images" not in m for m in saved if isinstance(m, dict))

            # メモリをクリア(再起動相当)しても復元できる
            appmain._code_ctx.clear()
            restored = appmain._load_or_init_ctx(cid, ws)
            assert any(m.get("role") == "tool" for m in restored if isinstance(m, dict))


def test_agent_http_blocks_when_changes_disabled():
    """allow_changes=False・計画モードでない → 変更はブロックされファイルは作られない。"""
    from fastapi.testclient import TestClient
    with temp_env() as ws:
        turns = [
            [_chunk(tool_calls=[_tc("write_file", {"path": "x.py", "content": "y=1\n"})])],
            [_chunk(content="変更は許可されていません")],
        ]
        client = TestClient(appmain.app)
        with patched(_impl, "_client", lambda: FakeClient(turns)), \
             patched(llm, "is_ollama_available", lambda: True), \
             patched(appmain, "_make_title", lambda c, m: ""):
            cid = _make_code_conv(client, ws, allow_changes=False, auto_accept_edits=False)
            r = client.post(f"/api/conversations/{cid}/agent", json={"content": "x.py を作って"})
            events = _sse_events(r.text)
            tr = [e for e in events if e.get("type") == "tool_result" and e.get("name") == "write_file"]
            assert tr and tr[0]["status"] == "blocked"
            assert not (ws / "x.py").exists()


def test_agent_http_rejects_non_code_conversation():
    from fastapi.testclient import TestClient
    with temp_env():
        client = TestClient(appmain.app)
        conv = client.post("/api/conversations", json={"kind": "chat"}).json()
        r = client.post(f"/api/conversations/{conv['id']}/agent", json={"content": "x"})
        assert r.status_code == 400


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
