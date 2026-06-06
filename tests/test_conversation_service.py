"""app.services.conversation_service の単体テスト(重い依存なし)。

一時 DB で CRUD・effective 付与・settings 部分マージ・workspace 検証・
メッセージ編集/削除を検証する。pytest でも `python tests/test_conversation_service.py` でも動く。
"""
import contextlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db                                     # noqa: E402
from app.services import conversation_service as cs     # noqa: E402


@contextlib.contextmanager
def temp_db():
    d = tempfile.mkdtemp(prefix="convsvc_test_")
    old = db.settings.db_path
    db.settings.db_path = Path(d) / "t.db"
    try:
        db.init_db()
        yield
    finally:
        db.settings.db_path = old
        shutil.rmtree(d, ignore_errors=True)


@contextlib.contextmanager
def patched(obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


# ---------------- with_effective / create ----------------
def test_with_effective_adds_key():
    with temp_db():
        c = db.create_conversation(title="x")
        out = cs.with_effective(c)
        assert "effective" in out and isinstance(out["effective"], dict)
        assert out["id"] == c["id"]


def test_create_defaults_title_by_kind_and_effective():
    with temp_db():
        chat = cs.create_conversation(None, "m", None, None, None, None)
        assert chat["title"] == "新しい会話"
        assert chat["kind"] == "chat"
        assert chat["model"] == "m"
        assert "effective" in chat
        code = cs.create_conversation(None, "m", None, None, None, "code")
        assert code["title"] == "新しいコード"
        assert code["kind"] == "code"


# ---------------- list / search ----------------
def test_list_and_search():
    with temp_db():
        cs.create_conversation("経費精算", "m", None, None, None, "chat")
        cs.create_conversation("無関係", "m", None, None, None, "chat")
        assert len(cs.list_conversations(None, None)) == 2
        hits = cs.list_conversations(None, "経費")
        assert [h["title"] for h in hits] == ["経費精算"]


# ---------------- get ----------------
def test_get_conversation_includes_messages_or_none():
    with temp_db():
        assert cs.get_conversation("missing") is None
        c = db.create_conversation(title="t")
        db.add_message(c["id"], "user", "やあ")
        out = cs.get_conversation(c["id"])
        assert out is not None
        assert "effective" in out
        assert [m["content"] for m in out["messages"]] == ["やあ"]


# ---------------- update ----------------
def test_update_missing_returns_none():
    with temp_db():
        assert cs.update_conversation("missing", {"title": "x"}) is None


def test_update_merges_settings_partially():
    with temp_db():
        c = db.create_conversation(title="t", settings_json={"temperature": 0.5, "top_p": 0.8})
        out = cs.update_conversation(c["id"], {"settings": {"temperature": 0.9}})
        assert out is not None
        merged = out["settings"]
        assert merged["temperature"] == 0.9   # 上書き
        assert merged["top_p"] == 0.8         # 既存は保持(部分マージ)


def test_update_rejects_bad_workspace():
    with temp_db(), patched(cs.safety, "check_workspace", lambda ws: (False, "禁止フォルダ")):
        c = db.create_conversation(title="t")
        try:
            cs.update_conversation(c["id"], {"settings": {"workspace": "/etc"}})
            assert False, "不正 workspace は ValueError になるべき"
        except ValueError as e:
            assert "禁止フォルダ" in str(e)


def test_update_accepts_ok_workspace():
    with temp_db(), patched(cs.safety, "check_workspace", lambda ws: (True, "")):
        c = db.create_conversation(title="t")
        out = cs.update_conversation(c["id"], {"settings": {"workspace": "/data/ok"}})
        assert out["settings"]["workspace"] == "/data/ok"


# ---------------- messages ----------------
def test_edit_message_and_truncate():
    with temp_db():
        c = db.create_conversation(title="t")
        m1 = db.add_message(c["id"], "user", "a")
        db.add_message(c["id"], "assistant", "b")
        db.add_message(c["id"], "user", "c")
        assert cs.edit_message(c["id"], m1["id"], "A", True) is True
        assert [m["content"] for m in db.list_messages(c["id"])] == ["A"]   # seq>=2 は削除


def test_edit_message_wrong_conv_returns_false():
    with temp_db():
        c1 = db.create_conversation(title="t1")
        c2 = db.create_conversation(title="t2")
        m = db.add_message(c1["id"], "user", "x")
        assert cs.edit_message(c2["id"], m["id"], "y", False) is False


def test_delete_message():
    with temp_db():
        c = db.create_conversation(title="t")
        m = db.add_message(c["id"], "user", "x")
        assert cs.delete_message(c["id"], m["id"]) is True
        assert db.get_message(m["id"]) is None
        assert cs.delete_message(c["id"], "nope") is False


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
