"""
auth.py
簡易パスワード認証。CHAT_PASSWORD が設定されている場合のみ有効。
署名付き Cookie(itsdangerous)でセッションを保持する。
"""
from __future__ import annotations

import hmac

from fastapi import Cookie, HTTPException, status
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

from .config import settings
from .logging_setup import get_logger

log = get_logger("auth")

COOKIE_NAME = "rag_session"
_MAX_AGE = 60 * 60 * 24 * 14  # 14日
_signer = TimestampSigner(settings.secret_key, salt="rag-session")


def verify_password(password: str) -> bool:
    """定数時間比較でパスワードを検証。"""
    if not settings.auth_enabled:
        return True
    return hmac.compare_digest(password or "", settings.password)


def make_session_token() -> str:
    return _signer.sign(b"authenticated").decode("utf-8")


def _token_valid(token: str | None) -> bool:
    if not token:
        return False
    try:
        _signer.unsign(token, max_age=_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


def is_authenticated(token: str | None) -> bool:
    """認証無効時は常に True。有効時はトークンを検証。"""
    if not settings.auth_enabled:
        return True
    return _token_valid(token)


async def require_auth(rag_session: str | None = Cookie(default=None)) -> None:
    """保護対象ルート用の依存関係。未認証なら 401。"""
    if not settings.auth_enabled:
        return
    if not _token_valid(rag_session):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="認証が必要です",
        )
