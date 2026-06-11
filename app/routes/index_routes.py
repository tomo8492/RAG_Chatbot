"""index_routes.py
参照資料インデックス(ナレッジベース)と一括要約の HTTP ルート。

ロジックは services.index_service に委譲し、ここは「検証 → 委譲 → 整形」に徹する。
ローカル LLM(Ollama)構成や挙動は従来のまま(main.py から無改変で切り出したもの)。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel

from .. import auth, db
from ..config import settings
from ..logging_setup import get_logger
from ..services import index_service

log = get_logger("index_routes")

# すべてのインデックス系ルートは認証必須(従来の per-route 依存と等価)
router = APIRouter(dependencies=[Depends(auth.require_auth)])


class IndexCreate(BaseModel):
    name: Optional[str] = None
    paths: list[str]


class SummarizeBody(BaseModel):
    instruction: str = ""
    model: Optional[str] = None
    map_model: Optional[str] = None
    categories: list[str] = []


@router.get("/api/indexes")
def api_list_indexes() -> list:
    return index_service.list_indexes()


@router.post("/api/indexes")
def api_create_index(body: IndexCreate) -> dict:
    if not body.paths:
        raise HTTPException(400, "フォルダが指定されていません")
    try:
        return index_service.create_index(body.name, body.paths)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/api/indexes/{iid}")
def api_get_index(iid: str) -> dict:
    idx = db.get_index(iid)
    if not idx:
        raise HTTPException(404, "インデックスが見つかりません")
    return idx


@router.post("/api/indexes/{iid}/rebuild")
def api_rebuild_index(iid: str) -> dict:
    idx = index_service.rebuild_index(iid)
    if not idx:
        raise HTTPException(404, "インデックスが見つかりません")
    return idx


@router.post("/api/indexes/{iid}/summarize")
def api_index_summarize(iid: str, body: SummarizeBody) -> Response:
    """資料フォルダ配下の全ファイルを map-reduce で一括要約(進捗をSSEで配信)。"""
    idx = db.get_index(iid)
    if not idx:
        raise HTTPException(404, "資料が見つかりません")
    gen = index_service.iter_summary_sse(idx, body.instruction, body.model,
                                         body.map_model, body.categories)
    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}
    return StreamingResponse(gen, media_type="text/event-stream", headers=headers)


@router.post("/api/indexes/{iid}/summarize/start")
def api_summarize_start(iid: str, body: SummarizeBody) -> dict:
    """裏(バックグラウンド)で一括要約を開始する。進捗は GET /summary でポーリング。"""
    idx = db.get_index(iid)
    if not idx:
        raise HTTPException(404, "資料が見つかりません")
    try:
        return index_service.start_summary_bg(idx, body.instruction, body.model,
                                              body.map_model, body.categories)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))


@router.get("/api/indexes/{iid}/summary")
def api_summary_status(iid: str) -> dict:
    return index_service.summary_status(iid)


@router.post("/api/indexes/{iid}/summary/cancel")
def api_summary_cancel(iid: str) -> dict:
    index_service.request_cancel(iid)
    return {"ok": True}


@router.delete("/api/indexes/{iid}")
def api_delete_index(iid: str) -> dict:
    if not index_service.delete_index(iid):
        raise HTTPException(404, "インデックスが見つかりません")
    return {"ok": True}


# 文書内画像のファイル名(内容ハッシュ + Web表示可能な拡張子)以外は配信しない
_IMG_NAME = re.compile(r"^[0-9a-f]{8,40}\.(?:png|jpe?g|gif|webp)$")


@router.get("/api/doc-images/{iid}/{name}")
def api_doc_image(iid: str, name: str) -> FileResponse:
    """チャンクに紐づく文書内画像(図)を配信する(認証必須・doc_images 配下に限定)。"""
    if not _IMG_NAME.match(name) or not re.match(r"^[0-9a-fA-F-]{8,64}$", iid):
        raise HTTPException(404, "画像が見つかりません")
    base = (settings.data_dir / "doc_images").resolve()
    p = (base / iid / name).resolve()
    if base not in p.parents or not p.is_file():
        raise HTTPException(404, "画像が見つかりません")
    return FileResponse(str(p), headers={"Cache-Control": "private, max-age=86400"})


# ------------------------------------------------------------------
#  手順ビューア(工程ごとの文章+画像)
# ------------------------------------------------------------------
def _resolve_index_file(idx: dict, path_str: str) -> Path:
    """ビューア対象ファイルが、この資料の登録フォルダ配下にあることを検証する。"""
    try:
        p = Path(path_str).resolve()
    except OSError:
        raise HTTPException(400, "パスを解決できません")
    for base in idx.get("paths") or []:
        try:
            b = Path(base).resolve()
        except OSError:
            continue
        if p == b or b in p.parents:
            if not p.is_file():
                raise HTTPException(404, "ファイルが見つかりません(移動/削除された可能性)")
            return p
    raise HTTPException(400, "この資料の登録フォルダ外のファイルは表示できません")


@router.get("/api/indexes/{iid}/files")
def api_index_files(iid: str) -> dict:
    """資料に含まれる対応ファイルの一覧(手順ビューアのファイル選択用)。"""
    idx = db.get_index(iid)
    if not idx:
        raise HTTPException(404, "インデックスが見つかりません")
    from .. import rag
    files = rag.scan_files(idx.get("paths") or [])[:500]
    return {"files": [{"name": f.name, "path": str(f)} for f in files]}


@router.get("/api/indexes/{iid}/procedure")
def api_index_procedure(iid: str, path: str) -> dict:
    """1ファイルを「工程ごと(文章+画像)」の構造で返す(手順ビューア本体)。"""
    idx = db.get_index(iid)
    if not idx:
        raise HTTPException(404, "インデックスが見つかりません")
    p = _resolve_index_file(idx, path)
    from .. import procedure
    try:
        return procedure.build_view(iid, p)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        log.exception("手順ビューの生成に失敗: %s", path)
        raise HTTPException(500, f"手順ビューの生成に失敗しました: {e}")
