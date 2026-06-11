"""app.db(SQLite 永続化)の単体テスト。

一時 DB に切り替えて、会話・メッセージの CRUD と検索(search_conversations)を
検証する。実際の本番 DB には触れない。pytest でも
`python tests/test_db.py` 単体実行でも動く。
"""
import contextlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402


@contextlib.contextmanager
def temp_db():
    """settings.db_path を一時ファイルへ差し替え、初期化して使う。終了時に復元。"""
    d = tempfile.mkdtemp(prefix="db_test_")
    old = db.settings.db_path
    db.settings.db_path = Path(d) / "t.db"
    try:
        db.init_db()
        yield
    finally:
        db.settings.db_path = old
        shutil.rmtree(d, ignore_errors=True)


# ---------------- conversation CRUD ----------------
def test_create_and_get_conversation():
    with temp_db():
        c = db.create_conversation(title="議事録", model="m", kind="chat")
        got = db.get_conversation(c["id"])
        assert got and got["title"] == "議事録" and got["kind"] == "chat"


def test_update_conversation_title():
    with temp_db():
        c = db.create_conversation(title="旧")
        db.update_conversation(c["id"], title="新")
        assert db.get_conversation(c["id"])["title"] == "新"


def test_delete_conversation():
    with temp_db():
        c = db.create_conversation(title="消す")
        db.delete_conversation(c["id"])
        assert db.get_conversation(c["id"]) is None


def test_list_conversations_by_kind():
    with temp_db():
        db.create_conversation(title="chat1", kind="chat")
        db.create_conversation(title="code1", kind="code")
        chats = db.list_conversations(kind="chat")
        assert [x["title"] for x in chats] == ["chat1"]


# ---------------- messages ----------------
def test_add_and_list_messages_in_seq_order():
    with temp_db():
        c = db.create_conversation()
        db.add_message(c["id"], "user", "最初")
        db.add_message(c["id"], "assistant", "次")
        msgs = db.list_messages(c["id"])
        assert [m["content"] for m in msgs] == ["最初", "次"]
        assert [m["seq"] for m in msgs] == [1, 2]


def test_update_message():
    with temp_db():
        c = db.create_conversation()
        m = db.add_message(c["id"], "user", "before")
        db.update_message(m["id"], "after")
        assert db.get_message(m["id"])["content"] == "after"


def test_delete_message():
    with temp_db():
        c = db.create_conversation()
        m = db.add_message(c["id"], "user", "x")
        db.delete_message(m["id"])
        assert db.get_message(m["id"]) is None


def test_delete_messages_from_seq():
    with temp_db():
        c = db.create_conversation()
        for i in range(1, 5):                 # seq 1..4
            db.add_message(c["id"], "user", f"m{i}")
        db.delete_messages_from(c["id"], 3)    # seq>=3 を削除
        assert [m["content"] for m in db.list_messages(c["id"])] == ["m1", "m2"]


# ---------------- search_conversations ----------------
def test_search_by_title():
    with temp_db():
        db.create_conversation(title="経費精算の手順")
        db.create_conversation(title="無関係")
        hits = db.search_conversations("経費")
        assert [h["title"] for h in hits] == ["経費精算の手順"]


def test_search_by_message_content():
    with temp_db():
        c = db.create_conversation(title="タイトルに無い語")
        db.add_message(c["id"], "user", "有給休暇の申請について")
        hits = db.search_conversations("有給")
        assert any(h["id"] == c["id"] for h in hits)


def test_search_distinct_when_multiple_messages_match():
    with temp_db():
        c = db.create_conversation(title="重複検証")
        db.add_message(c["id"], "user", "キーワード A")
        db.add_message(c["id"], "assistant", "キーワード B")
        hits = [h for h in db.search_conversations("キーワード") if h["id"] == c["id"]]
        assert len(hits) == 1                  # JOIN しても会話は1件にまとまる


def test_search_filters_by_kind():
    with temp_db():
        db.create_conversation(title="共通語 chat", kind="chat")
        db.create_conversation(title="共通語 code", kind="code")
        hits = db.search_conversations("共通語", kind="code")
        assert [h["title"] for h in hits] == ["共通語 code"]


def test_search_no_match_returns_empty():
    with temp_db():
        db.create_conversation(title="あ")
        assert db.search_conversations("該当なし") == []


# ---------------- お気に入り(pinned: サイドバー上部に固定) ----------------
def test_pinned_defaults_false_and_toggles():
    with temp_db():
        c = db.create_conversation(title="A")
        assert c["pinned"] is False
        got = db.update_conversation(c["id"], pinned=True)
        assert got is not None and got["pinned"] is True
        got = db.update_conversation(c["id"], pinned=False)
        assert got is not None and got["pinned"] is False


def test_list_conversations_pinned_first():
    with temp_db():
        old = db.create_conversation(title="古い(固定)")
        db.create_conversation(title="中間")
        db.create_conversation(title="最新")
        db.update_conversation(old["id"], pinned=True)
        titles = [c["title"] for c in db.list_conversations()]
        assert titles[0] == "古い(固定)"            # 固定が最上部
        assert titles.index("最新") < titles.index("中間")   # 残りは更新順
        assert db.list_conversations()[0]["pinned"] is True
        # kind 絞り込みでも固定が先頭
        code = db.create_conversation(title="コード会話", kind="code")
        db.update_conversation(code["id"], pinned=True)
        db.create_conversation(title="コード新規", kind="code")
        assert db.list_conversations(kind="code")[0]["title"] == "コード会話"


def test_search_conversations_pinned_first():
    with temp_db():
        a = db.create_conversation(title="日当の規程メモ")
        db.create_conversation(title="日当の質問ログ")
        db.update_conversation(a["id"], pinned=True)
        got = db.search_conversations("日当")
        assert got and got[0]["id"] == a["id"]


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
