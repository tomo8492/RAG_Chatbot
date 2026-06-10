"""fs_routes.py
ファイルシステム閲覧(参照資料フォルダの選択)。ロジックは fsbrowse へ委譲。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException

from .. import auth
from ..fsbrowse import count_supported_recursive, get_roots, list_dir
from ..logging_setup import get_logger

log = get_logger("fs")

router = APIRouter(dependencies=[Depends(auth.require_auth)])


@router.get("/api/fs/roots")
def api_fs_roots() -> dict:
    return {"roots": get_roots()}


@router.get("/api/fs")
def api_fs(path: Optional[str] = None) -> dict:
    try:
        return list_dir(path)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except Exception as e:
        log.exception("フォルダ一覧取得失敗: %s", path)
        raise HTTPException(400, f"このフォルダは開けません: {e}")


@router.post("/api/fs/estimate")
def api_fs_estimate(paths: list[str] = Body(..., embed=True)) -> dict:
    count, capped = count_supported_recursive(paths)
    return {"count": count, "capped": capped}
