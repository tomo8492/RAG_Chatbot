"""
main.py
FastAPI 本体。API ルートと SSE ストリーミング生成。
"""
from __future__ import annotations

import ipaddress
import json
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import Body, Cookie, Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, db, export, llm, rag
from .config import settings
from .defaults import effective_for, get_defaults, set_defaults
from .fsbrowse import count_supported_recursive, get_roots, list_dir
from .logging_setup import get_logger, setup_logging

setup_logging()
log = get_logger("main")

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    log.info("===== %s 起動 =====", settings.app_title)
    log.info("認証: %s", "有効(パスワードあり)" if settings.auth_enabled else "無効(誰でもアクセス可)")
    if not llm.is_ollama_available():
        log.warning("Ollama に接続できません(%s)。`ollama serve` を確認してください。",
                    settings.ollama_host)
    yield


app = FastAPI(title=settings.app_title, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _ip_allowed(host: str | None) -> bool:
    """ループバック / プライベートLAN / リンクローカルのみ許可。"""
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host == "localhost"
    return ip.is_loopback or ip.is_private or ip.is_link_local


@app.middleware("http")
async def _lan_guard(request, call_next):
    if settings.lan_only:
        client = request.client
        host = client.host if client else None
        if not _ip_allowed(host):
            log.warning("LAN外からのアクセスを拒否: %s", host)
            return JSONResponse(
                {"detail": "このネットワークからはアクセスできません(LAN制限が有効です)"},
                status_code=403,
            )
    return await call_next(request)


# ============================================================
#  SSE ヘルパ
# ============================================================
def sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


# ============================================================
#  認証
# ============================================================
class LoginBody(BaseModel):
    password: str = ""


@app.get("/api/config")
def api_config(rag_session: Optional[str] = Cookie(default=None)) -> dict:
    return {
        "app_title": settings.app_title,
        "auth_enabled": settings.auth_enabled,
        "authenticated": auth.is_authenticated(rag_session),
        "ollama_available": llm.is_ollama_available(),
        "embed_backend": settings.embed_backend,
        "embed_model": settings.embed_model,
    }


@app.post("/api/login")
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


@app.post("/api/logout")
def api_logout() -> Response:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE_NAME)
    return resp


# ============================================================
#  設定(グローバル既定値)
# ============================================================
@app.get("/api/settings", dependencies=[Depends(auth.require_auth)])
def api_get_settings() -> dict:
    return get_defaults()


@app.patch("/api/settings", dependencies=[Depends(auth.require_auth)])
def api_patch_settings(patch: dict = Body(...)) -> dict:
    return set_defaults(patch)


@app.get("/api/models", dependencies=[Depends(auth.require_auth)])
def api_models() -> dict:
    return {"available": llm.is_ollama_available(), "models": llm.list_models()}


# ============================================================
#  会話
# ============================================================
class ConvCreate(BaseModel):
    title: Optional[str] = None
    model: Optional[str] = None
    system_prompt: Optional[str] = None
    active_indexes: Optional[list] = None
    settings: Optional[dict] = None


class ConvUpdate(BaseModel):
    title: Optional[str] = None
    model: Optional[str] = None
    system_prompt: Optional[str] = None
    active_indexes: Optional[list] = None
    settings: Optional[dict] = None


@app.get("/api/conversations", dependencies=[Depends(auth.require_auth)])
def api_list_conversations() -> list:
    return db.list_conversations()


@app.post("/api/conversations", dependencies=[Depends(auth.require_auth)])
def api_create_conversation(body: ConvCreate) -> dict:
    d = get_defaults()
    conv = db.create_conversation(
        title=body.title or "新しい会話",
        model=body.model or d["model"],
        system_prompt=body.system_prompt,
        settings_json=body.settings or {},
        active_indexes=body.active_indexes or [],
    )
    return _conv_with_effective(conv)


@app.get("/api/conversations/{cid}", dependencies=[Depends(auth.require_auth)])
def api_get_conversation(cid: str) -> dict:
    conv = db.get_conversation(cid)
    if not conv:
        raise HTTPException(404, "会話が見つかりません")
    out = _conv_with_effective(conv)
    out["messages"] = db.list_messages(cid)
    return out


@app.patch("/api/conversations/{cid}", dependencies=[Depends(auth.require_auth)])
def api_update_conversation(cid: str, body: ConvUpdate) -> dict:
    conv = db.get_conversation(cid)
    if not conv:
        raise HTTPException(404, "会話が見つかりません")
    fields = {k: v for k, v in body.dict().items() if v is not None}
    # settings は部分マージ
    if "settings" in fields:
        merged = dict(conv.get("settings") or {})
        merged.update(fields["settings"])
        fields["settings"] = merged
    conv = db.update_conversation(cid, **fields)
    return _conv_with_effective(conv)


@app.delete("/api/conversations/{cid}", dependencies=[Depends(auth.require_auth)])
def api_delete_conversation(cid: str) -> dict:
    if not db.get_conversation(cid):
        raise HTTPException(404, "会話が見つかりません")
    rag.delete_conv_collection(cid)
    db.delete_conversation(cid)
    return {"ok": True}


def _conv_with_effective(conv: dict) -> dict:
    out = dict(conv)
    out["effective"] = effective_for(conv)
    return out


# ============================================================
#  添付ファイル(会話コレクションに埋め込み)
# ============================================================
@app.post("/api/conversations/{cid}/attachments", dependencies=[Depends(auth.require_auth)])
async def api_attach(cid: str, file: UploadFile = File(...)) -> dict:
    conv = db.get_conversation(cid)
    if not conv:
        raise HTTPException(404, "会話が見つかりません")

    limit = settings.max_upload_mb * 1024 * 1024
    safe = f"{uuid.uuid4().hex}_{Path(file.filename).name}"
    dest = settings.upload_dir / safe
    size = 0
    with dest.open("wb") as f:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                f.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, f"ファイルが大きすぎます(上限 {settings.max_upload_mb}MB)")
            f.write(chunk)

    try:
        chunks = rag.add_attachment(cid, dest, Path(file.filename).name)
    except Exception as e:
        log.exception("添付処理失敗")
        raise HTTPException(500, f"添付の処理に失敗しました: {e}")

    if chunks == 0:
        raise HTTPException(422, "テキストを抽出できませんでした(対応形式か確認してください)")

    log.info("添付: %s (%dチャンク) -> 会話 %s", file.filename, chunks, cid)
    return {"name": Path(file.filename).name, "chunks": chunks}


# ============================================================
#  生成(SSE ストリーミング)
# ============================================================
class GenerateBody(BaseModel):
    content: str = ""
    attachments: list[str] = []
    mode: str = "send"   # send | regenerate


@app.post("/api/conversations/{cid}/generate", dependencies=[Depends(auth.require_auth)])
def api_generate(cid: str, body: GenerateBody) -> Response:
    conv = db.get_conversation(cid)
    if not conv:
        raise HTTPException(404, "会話が見つかりません")

    eff = effective_for(conv)
    mode = body.mode

    # --- 対象クエリとユーザーメッセージの確定 ---
    user_msg = None
    if mode == "regenerate":
        msgs = db.list_messages(cid)
        # 末尾の assistant 群を削除し、最後の user を対象にする
        last_user = None
        for m in reversed(msgs):
            if m["role"] == "user":
                last_user = m
                break
        if not last_user:
            raise HTTPException(400, "再生成できるメッセージがありません")
        db.delete_messages_from(cid, last_user["seq"] + 1)
        query = last_user["content"]
    else:
        content = (body.content or "").strip()
        if not content:
            raise HTTPException(400, "メッセージが空です")
        user_msg = db.add_message(cid, "user", content, attachments=body.attachments)
        query = content
        # 初回メッセージならタイトルを自動設定
        if (conv.get("title") in (None, "", "新しい会話")):
            title = content.strip().splitlines()[0][:30]
            db.update_conversation(cid, title=title or "新しい会話")

    # --- RAG 検索 ---
    sources: list[dict] = []
    hits: list[dict] = []
    try:
        hits = rag.retrieve(query, conv.get("active_indexes", []), cid, int(eff["top_k"]))
    except Exception:
        log.exception("検索失敗(無視して続行)")
    if hits:
        seen = set()
        for h in hits:
            key = (h["source"], h["loc"])
            if key not in seen:
                seen.add(key)
                sources.append({"source": h["source"], "loc": h["loc"],
                                "score": round(h["score"], 3), "attachment": h["attachment"]})

    history = db.list_messages(cid)
    messages = llm.build_messages(eff["system_prompt"], history, hits)
    model = eff["model"]

    def gen():
        if user_msg:
            yield sse({"type": "user_saved", "message": user_msg})
        if not model:
            yield sse({"type": "error", "error": "モデルが選択されていません。設定でモデルを指定してください。"})
            return
        if not llm.is_ollama_available():
            yield sse({"type": "error",
                       "error": f"Ollama に接続できません({settings.ollama_host})。`ollama serve` を起動してください。"})
            return
        if sources:
            yield sse({"type": "sources", "sources": sources})

        acc_content, acc_thinking = "", ""
        saved = False
        log.info("生成開始 [conv=%s model=%s effort=%s top_k=%s] Q=%s",
                 cid, model, eff["effort"], eff["top_k"], query[:60])
        try:
            for ev in llm.chat_stream(
                messages, model,
                temperature=float(eff["temperature"]), top_p=float(eff["top_p"]),
                num_predict=int(eff["num_predict"]),
                num_ctx=int(eff["num_ctx"]) or None,
                effort=str(eff["effort"]),
            ):
                if ev["type"] == "thinking":
                    acc_thinking += ev["text"]
                    yield sse({"type": "thinking", "delta": ev["text"]})
                else:
                    acc_content += ev["text"]
                    yield sse({"type": "content", "delta": ev["text"]})
            asst = db.add_message(cid, "assistant", acc_content, sources=sources)
            saved = True
            log.info("生成完了 [conv=%s] %d文字", cid, len(acc_content))
            yield sse({"type": "done", "message": asst})
        except GeneratorExit:
            # クライアント切断(停止ボタン)
            log.info("生成停止(クライアント切断)[conv=%s]", cid)
            raise
        except Exception as e:
            log.exception("生成エラー")
            yield sse({"type": "error", "error": str(e)})
        finally:
            if not saved and acc_content.strip():
                db.add_message(cid, "assistant", acc_content, sources=sources)

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}
    return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)


# ============================================================
#  インデックス(ナレッジベース)
# ============================================================
class IndexCreate(BaseModel):
    name: Optional[str] = None
    paths: list[str]


def _build_async(iid: str, paths: list[str]) -> None:
    threading.Thread(target=rag.build_index, args=(iid, paths), daemon=True).start()


@app.get("/api/indexes", dependencies=[Depends(auth.require_auth)])
def api_list_indexes() -> list:
    return db.list_indexes()


@app.post("/api/indexes", dependencies=[Depends(auth.require_auth)])
def api_create_index(body: IndexCreate) -> dict:
    if not body.paths:
        raise HTTPException(400, "フォルダが指定されていません")
    name = body.name or (Path(body.paths[0]).name or body.paths[0])
    idx = db.create_index(name, body.paths)
    _build_async(idx["id"], body.paths)
    log.info("インデックス作成開始: %s (%s)", name, body.paths)
    return idx


@app.get("/api/indexes/{iid}", dependencies=[Depends(auth.require_auth)])
def api_get_index(iid: str) -> dict:
    idx = db.get_index(iid)
    if not idx:
        raise HTTPException(404, "インデックスが見つかりません")
    return idx


@app.post("/api/indexes/{iid}/rebuild", dependencies=[Depends(auth.require_auth)])
def api_rebuild_index(iid: str) -> dict:
    idx = db.get_index(iid)
    if not idx:
        raise HTTPException(404, "インデックスが見つかりません")
    db.update_index(iid, status="building", error=None)
    _build_async(iid, idx["paths"])
    return db.get_index(iid)


@app.delete("/api/indexes/{iid}", dependencies=[Depends(auth.require_auth)])
def api_delete_index(iid: str) -> dict:
    if not db.get_index(iid):
        raise HTTPException(404, "インデックスが見つかりません")
    rag.delete_index_collection(iid)
    db.delete_index(iid)
    return {"ok": True}


# ============================================================
#  ファイルシステム閲覧(フォルダ選択)
# ============================================================
@app.get("/api/fs/roots", dependencies=[Depends(auth.require_auth)])
def api_fs_roots() -> dict:
    return {"roots": get_roots()}


@app.get("/api/fs", dependencies=[Depends(auth.require_auth)])
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


@app.post("/api/fs/estimate", dependencies=[Depends(auth.require_auth)])
def api_fs_estimate(paths: list[str] = Body(..., embed=True)) -> dict:
    count, capped = count_supported_recursive(paths)
    return {"count": count, "capped": capped}


# ============================================================
#  ファイル出力(回答の保存)
# ============================================================
class ExportBody(BaseModel):
    content: str
    format: str = "md"        # md|txt|html|docx|xlsx|pptx|code
    ext: Optional[str] = None  # format=code のときの拡張子(例: bas)
    title: Optional[str] = "回答"


def _safe_stem(title: str) -> str:
    stem = re.sub(r"[\\/:*?\"<>|\n\r\t]", "", (title or "回答")).strip()
    return (stem[:40] or "回答")


@app.post("/api/export", dependencies=[Depends(auth.require_auth)])
def api_export(body: ExportBody) -> Response:
    try:
        data, mime, ext = export.export_content(body.content, body.format, body.ext, body.title or "回答")
    except ValueError as e:
        raise HTTPException(400, str(e))
    except ImportError as e:
        raise HTTPException(500, f"変換に必要なライブラリが未導入です: {e}")
    except Exception as e:
        log.exception("エクスポート失敗")
        raise HTTPException(500, f"変換に失敗しました: {e}")

    fname = f"{_safe_stem(body.title or '回答')}.{ext}"
    log.info("エクスポート: format=%s -> %s (%d bytes)", body.format, fname, len(data))
    headers = {
        "Content-Disposition": f"attachment; filename=\"export.{ext}\"; filename*=UTF-8''{quote(fname)}",
        "X-Filename": quote(fname),
        "Access-Control-Expose-Headers": "X-Filename",
    }
    return Response(content=data, media_type=mime, headers=headers)


# ============================================================
#  フロントエンド
# ============================================================
@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "ts": time.time()}
