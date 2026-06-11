"""conversation_routes.py
会話・メッセージの CRUD ルート(会話の削除を除く)。ロジックは conversation_service へ委譲。

注: DELETE /api/conversations/{cid}(会話削除)は Code エージェントの実行時状態の掃除を
伴うため main.py に残置している。生成(generate)・添付・エージェントも別ドメインとして main.py。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import auth
from ..services import conversation_service

router = APIRouter(dependencies=[Depends(auth.require_auth)])


class ConvCreate(BaseModel):
    title: Optional[str] = None
    model: Optional[str] = None
    system_prompt: Optional[str] = None
    active_indexes: Optional[list] = None
    settings: Optional[dict] = None
    kind: Optional[str] = None       # chat | code


class ConvUpdate(BaseModel):
    title: Optional[str] = None
    model: Optional[str] = None
    system_prompt: Optional[str] = None
    active_indexes: Optional[list] = None
    settings: Optional[dict] = None
    pinned: Optional[bool] = None   # お気に入り(サイドバー上部に固定)


class MsgEditBody(BaseModel):
    content: str
    truncate_after: bool = False   # True=このメッセージ以降を削除(編集して再送する用)


@router.get("/api/conversations")
def api_list_conversations(kind: Optional[str] = None, q: Optional[str] = None) -> list:
    return conversation_service.list_conversations(kind, q)


@router.post("/api/conversations")
def api_create_conversation(body: ConvCreate) -> dict:
    return conversation_service.create_conversation(
        body.title, body.model, body.system_prompt,
        body.active_indexes, body.settings, body.kind)


@router.get("/api/conversations/{cid}")
def api_get_conversation(cid: str) -> dict:
    out = conversation_service.get_conversation(cid)
    if out is None:
        raise HTTPException(404, "会話が見つかりません")
    return out


@router.patch("/api/conversations/{cid}")
def api_update_conversation(cid: str, body: ConvUpdate) -> dict:
    fields = {k: v for k, v in body.dict().items() if v is not None}
    try:
        out = conversation_service.update_conversation(cid, fields)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if out is None:
        raise HTTPException(404, "会話が見つかりません")
    return out


@router.patch("/api/conversations/{cid}/messages/{mid}")
def api_edit_message(cid: str, mid: str, body: MsgEditBody) -> dict:
    if not conversation_service.edit_message(cid, mid, body.content, body.truncate_after):
        raise HTTPException(404, "メッセージが見つかりません")
    return {"ok": True}


@router.delete("/api/conversations/{cid}/messages/{mid}")
def api_delete_message(cid: str, mid: str) -> dict:
    if not conversation_service.delete_message(cid, mid):
        raise HTTPException(404, "メッセージが見つかりません")
    return {"ok": True}
