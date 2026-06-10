"""meta_routes.py
アプリ設定・認証・グローバル既定値・モデル一覧のルート。
ロジックは既存モジュール(auth / defaults / llm)へ委譲する薄い層。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, Cookie, Depends, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from .. import auth, llm
from ..config import settings
from ..defaults import get_defaults, set_defaults

router = APIRouter()


class LoginBody(BaseModel):
    password: str = ""


@router.get("/api/config")
def api_config(rag_session: Optional[str] = Cookie(default=None)) -> dict:
    return {
        "app_title": settings.app_title,
        "auth_enabled": settings.auth_enabled,
        "authenticated": auth.is_authenticated(rag_session),
        "ollama_available": llm.is_ollama_available(),
        "embed_backend": settings.embed_backend,
        "embed_model": settings.embed_model,
    }


@router.post("/api/login")
def api_login(body: LoginBody) -> Response:
    if not settings.auth_enabled:
        return JSONResponse({"ok": True})
    if not auth.verify_password(body.password):
        raise HTTPException(status_code=401, detail="パスワードが違います")
    token = auth.make_session_token()
    resp = JSONResponse({"ok": True})
    resp.set_cookie(auth.COOKIE_NAME, token, httponly=True, samesite="lax",
                    max_age=60 * 60 * 24 * 14)
    return resp


@router.post("/api/logout")
def api_logout() -> Response:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE_NAME)
    return resp


@router.get("/api/settings", dependencies=[Depends(auth.require_auth)])
def api_get_settings() -> dict:
    return get_defaults()


@router.patch("/api/settings", dependencies=[Depends(auth.require_auth)])
def api_patch_settings(patch: dict = Body(...)) -> dict:
    return set_defaults(patch)


@router.get("/api/models", dependencies=[Depends(auth.require_auth)])
def api_models() -> dict:
    return {"available": llm.is_ollama_available(), "models": llm.list_models()}
