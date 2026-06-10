"""conversation_service.py
会話・メッセージのビジネスロジック(CRUD)。FastAPI/HTTP には依存しない。

注: 会話の「削除」は Code エージェントの実行時状態(_code_ctx 等, agent ドメイン)の
掃除を伴うため main.py 側に残置している(agent 状態を切り出す別スライスで移設予定)。
"""
from __future__ import annotations

from typing import Optional

from .. import db, safety
from ..defaults import effective_for, model_for_kind


def with_effective(conv: dict) -> dict:
    out = dict(conv)
    out["effective"] = effective_for(conv)
    return out


def list_conversations(kind: Optional[str], q: Optional[str]) -> list:
    if q and q.strip():
        return db.search_conversations(q.strip(), kind=kind)   # タイトル+本文を検索
    return db.list_conversations(kind=kind)


def create_conversation(title, model, system_prompt, active_indexes, settings, kind) -> dict:
    kind = kind or "chat"
    conv = db.create_conversation(
        title=title or ("新しいコード" if kind == "code" else "新しい会話"),
        model=model or model_for_kind(kind),
        system_prompt=system_prompt,
        settings_json=settings or {},
        active_indexes=active_indexes or [],
        kind=kind,
    )
    return with_effective(conv)


def get_conversation(cid: str) -> Optional[dict]:
    """会話本体 + effective 設定 + メッセージ一覧。見つからなければ None。"""
    conv = db.get_conversation(cid)
    if not conv:
        return None
    out = with_effective(conv)
    out["messages"] = db.list_messages(cid)
    return out


def update_conversation(cid: str, fields: dict) -> Optional[dict]:
    """fields は None を除いた更新項目。会話が無ければ None を返す。

    settings は部分マージし、Code の作業フォルダ(workspace)は安全なフォルダのみ許可。
    不正な workspace は ValueError(呼び出し側で 400 に変換)。
    """
    conv = db.get_conversation(cid)
    if not conv:
        return None
    if "settings" in fields:
        # Code の作業フォルダは安全なフォルダのみ許可(OS/システム等は不可)
        ws = (fields["settings"] or {}).get("workspace")
        if ws:
            ok, reason = safety.check_workspace(ws)
            if not ok:
                raise ValueError(f"このフォルダは作業フォルダに設定できません: {reason}")
        merged = dict(conv.get("settings") or {})
        merged.update(fields["settings"])
        fields["settings"] = merged
    updated = db.update_conversation(cid, **fields)
    return with_effective(updated) if updated else None


def edit_message(cid: str, mid: str, content: str, truncate_after: bool) -> bool:
    """メッセージを編集。truncate_after なら以降を削除。対象が無ければ False。"""
    m = db.get_message(mid)
    if not m or m.get("conversation_id") != cid:
        return False
    db.update_message(mid, content)
    if truncate_after:
        db.delete_messages_from(cid, int(m["seq"]) + 1)   # 以降を削除(再生成で続けられる)
    return True


def delete_message(cid: str, mid: str) -> bool:
    """メッセージを削除。対象が無ければ False。"""
    m = db.get_message(mid)
    if not m or m.get("conversation_id") != cid:
        return False
    db.delete_message(mid)
    return True
