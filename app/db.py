"""
db.py
SQLite による永続化。
  - conversations : 会話スレッド
  - messages      : 各メッセージ
  - indexes       : 参照資料インデックス(ナレッジベース)
スレッド安全のため、呼び出しごとに接続を開く方式を採用。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from .config import settings
from .logging_setup import get_logger

log = get_logger("db")
_write_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uid() -> str:
    return uuid.uuid4().hex


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(settings.db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    with _write_lock, _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id            TEXT PRIMARY KEY,
                title         TEXT NOT NULL DEFAULT '新しい会話',
                model         TEXT,
                system_prompt TEXT,
                active_indexes TEXT NOT NULL DEFAULT '[]',
                settings      TEXT NOT NULL DEFAULT '{}',
                kind          TEXT NOT NULL DEFAULT 'chat',
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS kv (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                id              TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                role            TEXT NOT NULL,
                content         TEXT NOT NULL,
                sources         TEXT NOT NULL DEFAULT '[]',
                attachments     TEXT NOT NULL DEFAULT '[]',
                created_at      TEXT NOT NULL,
                seq             INTEGER NOT NULL,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, seq);

            CREATE TABLE IF NOT EXISTS indexes (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                paths       TEXT NOT NULL DEFAULT '[]',
                status      TEXT NOT NULL DEFAULT 'building',
                file_count  INTEGER NOT NULL DEFAULT 0,
                chunk_count INTEGER NOT NULL DEFAULT 0,
                error       TEXT,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );
            """
        )
        # 既存DBへの後方互換マイグレーション(kind 列の追加)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(conversations)").fetchall()}
        if "kind" not in cols:
            conn.execute("ALTER TABLE conversations ADD COLUMN kind TEXT NOT NULL DEFAULT 'chat'")
        conn.commit()
    log.info("DB 初期化完了: %s", settings.db_path)


# ============================================================
#  会話
# ============================================================
def create_conversation(title: str = "新しい会話", model: Optional[str] = None,
                        system_prompt: Optional[str] = None,
                        settings_json: Optional[dict] = None,
                        active_indexes: Optional[list] = None,
                        kind: str = "chat") -> dict:
    cid = _uid()
    now = _now()
    with _write_lock, _connect() as conn:
        conn.execute(
            "INSERT INTO conversations (id, title, model, system_prompt, active_indexes, settings, kind, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (cid, title, model, system_prompt,
             json.dumps(active_indexes or [], ensure_ascii=False),
             json.dumps(settings_json or {}, ensure_ascii=False),
             kind or "chat",
             now, now),
        )
        conn.commit()
    return get_conversation(cid)


def get_conversation(cid: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
    return _conv_to_dict(row) if row else None


def list_conversations(kind: Optional[str] = None) -> list[dict]:
    with _connect() as conn:
        if kind:
            rows = conn.execute(
                "SELECT * FROM conversations WHERE kind=? ORDER BY updated_at DESC", (kind,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM conversations ORDER BY updated_at DESC"
            ).fetchall()
    return [_conv_to_dict(r) for r in rows]


def update_conversation(cid: str, **fields: Any) -> Optional[dict]:
    allowed = {"title", "model", "system_prompt", "active_indexes", "settings"}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k in ("active_indexes", "settings"):
            v = json.dumps(v, ensure_ascii=False)
        sets.append(f"{k}=?")
        vals.append(v)
    if not sets:
        return get_conversation(cid)
    sets.append("updated_at=?")
    vals.append(_now())
    vals.append(cid)
    with _write_lock, _connect() as conn:
        conn.execute(f"UPDATE conversations SET {', '.join(sets)} WHERE id=?", vals)
        conn.commit()
    return get_conversation(cid)


def touch_conversation(cid: str) -> None:
    with _write_lock, _connect() as conn:
        conn.execute("UPDATE conversations SET updated_at=? WHERE id=?", (_now(), cid))
        conn.commit()


def delete_conversation(cid: str) -> None:
    with _write_lock, _connect() as conn:
        conn.execute("DELETE FROM conversations WHERE id=?", (cid,))
        conn.commit()


def _conv_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["active_indexes"] = json.loads(d.get("active_indexes") or "[]")
    d["settings"] = json.loads(d.get("settings") or "{}")
    d.setdefault("kind", "chat")
    return d


# ============================================================
#  グローバル設定 (kv)
# ============================================================
def get_kv(key: str, default: Any = None) -> Any:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return default


def set_kv(key: str, value: Any) -> None:
    with _write_lock, _connect() as conn:
        conn.execute(
            "INSERT INTO kv (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        conn.commit()


def get_all_kv() -> dict:
    with _connect() as conn:
        rows = conn.execute("SELECT key, value FROM kv").fetchall()
    out = {}
    for r in rows:
        try:
            out[r["key"]] = json.loads(r["value"])
        except (json.JSONDecodeError, TypeError):
            out[r["key"]] = r["value"]
    return out


# ============================================================
#  メッセージ
# ============================================================
def add_message(conversation_id: str, role: str, content: str,
                sources: Optional[list] = None, attachments: Optional[list] = None) -> dict:
    mid = _uid()
    now = _now()
    with _write_lock, _connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM messages WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()
        seq = (row["m"] or 0) + 1
        conn.execute(
            "INSERT INTO messages (id, conversation_id, role, content, sources, attachments, created_at, seq)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (mid, conversation_id, role, content,
             json.dumps(sources or [], ensure_ascii=False),
             json.dumps(attachments or [], ensure_ascii=False),
             now, seq),
        )
        conn.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id))
        conn.commit()
    return get_message(mid)


def get_message(mid: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
    return _msg_to_dict(row) if row else None


def list_messages(conversation_id: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY seq ASC",
            (conversation_id,),
        ).fetchall()
    return [_msg_to_dict(r) for r in rows]


def update_message(mid: str, content: str, sources: Optional[list] = None) -> Optional[dict]:
    with _write_lock, _connect() as conn:
        if sources is None:
            conn.execute("UPDATE messages SET content=? WHERE id=?", (content, mid))
        else:
            conn.execute(
                "UPDATE messages SET content=?, sources=? WHERE id=?",
                (content, json.dumps(sources, ensure_ascii=False), mid),
            )
        conn.commit()
    return get_message(mid)


def delete_message(mid: str) -> None:
    with _write_lock, _connect() as conn:
        conn.execute("DELETE FROM messages WHERE id=?", (mid,))
        conn.commit()


def delete_messages_from(conversation_id: str, seq: int) -> None:
    """指定 seq 以降のメッセージを削除(再生成・編集時に使用)。"""
    with _write_lock, _connect() as conn:
        conn.execute(
            "DELETE FROM messages WHERE conversation_id=? AND seq>=?",
            (conversation_id, seq),
        )
        conn.commit()


def _msg_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["sources"] = json.loads(d.get("sources") or "[]")
    d["attachments"] = json.loads(d.get("attachments") or "[]")
    return d


# ============================================================
#  インデックス(ナレッジベース)
# ============================================================
def create_index(name: str, paths: list[str]) -> dict:
    iid = _uid()
    now = _now()
    with _write_lock, _connect() as conn:
        conn.execute(
            "INSERT INTO indexes (id, name, paths, status, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (iid, name, json.dumps(paths, ensure_ascii=False), "building", now, now),
        )
        conn.commit()
    return get_index(iid)


def get_index(iid: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM indexes WHERE id=?", (iid,)).fetchone()
    return _index_to_dict(row) if row else None


def list_indexes() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM indexes ORDER BY created_at DESC").fetchall()
    return [_index_to_dict(r) for r in rows]


def update_index(iid: str, **fields: Any) -> Optional[dict]:
    allowed = {"name", "status", "file_count", "chunk_count", "error", "paths"}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "paths":
            v = json.dumps(v, ensure_ascii=False)
        sets.append(f"{k}=?")
        vals.append(v)
    if not sets:
        return get_index(iid)
    sets.append("updated_at=?")
    vals.append(_now())
    vals.append(iid)
    with _write_lock, _connect() as conn:
        conn.execute(f"UPDATE indexes SET {', '.join(sets)} WHERE id=?", vals)
        conn.commit()
    return get_index(iid)


def delete_index(iid: str) -> None:
    with _write_lock, _connect() as conn:
        conn.execute("DELETE FROM indexes WHERE id=?", (iid,))
        conn.commit()


def _index_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["paths"] = json.loads(d.get("paths") or "[]")
    return d
