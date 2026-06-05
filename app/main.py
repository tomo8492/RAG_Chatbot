"""
main.py
FastAPI 本体。API ルートと SSE ストリーミング生成。
"""
from __future__ import annotations

import base64
import hmac
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

from fastapi import Body, Cookie, Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agent, auth, db, export, llm, postprocess, rag, safety, summarize
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
    log.info("アクセス制限(LAN_ONLY): %s",
             "有効(社内/ローカルネットワークのみ)" if settings.lan_only else "無効(全ネットワーク)")
    if settings.allowed_cidrs:
        log.info("追加許可ネットワーク(ALLOWED_CIDRS): %s",
                 ", ".join(str(n) for n in settings.allowed_cidrs))
    if not settings.lan_only:
        log.warning("LAN_ONLY が無効です。社外からの接続を遮断するには .env の LAN_ONLY=true(既定)にしてください。")
    if not settings.auth_enabled:
        log.warning("パスワード未設定です。ネットワーク内の誰でも利用でき、Code(コーディング"
                    "エージェント)はサーバ上でコマンド実行・ファイル変更が可能になります。"
                    ".env の CHAT_PASSWORD 設定を強く推奨します。")
    if not llm.is_ollama_available():
        log.warning("Ollama に接続できません(%s)。`ollama serve` を確認してください。",
                    settings.ollama_host)
    yield


app = FastAPI(title=settings.app_title, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _ip_allowed(host: str | None) -> bool:
    """ループバック / プライベートLAN / リンクローカル / 追加許可レンジで許可。

    RFC1918 以外で社内として許可したいレンジ(例 172.36.x.x)は、コードに埋め込まず
    .env の ALLOWED_CIDRS(git管理外=より安全)で指定する。
    """
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host == "localhost"
    if ip.is_loopback or ip.is_private or ip.is_link_local:
        return True
    for net in settings.allowed_cidrs:  # .env の ALLOWED_CIDRS による追加許可(社内の非標準レンジ等)
        if ip.version == net.version and ip in net:
            return True
    return False


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


def _make_title(content: str, model: str) -> str:
    """最初のユーザーメッセージから、短い日本語タイトルをLLMで生成(失敗時 '')。"""
    src = (content or "").strip()
    if not src:
        return ""
    prompt = ("次のメッセージに、日本語の短いタイトルを1つだけ付けてください"
              "(全角18字以内・体言止め・記号や引用符や句点なし・前置きや説明は書かない):\n\n" + src[:500])
    raw = llm.complete_text(prompt, model, num_predict=32,
                            system="会話に短いタイトルだけを返すアシスタント。タイトル本文のみを出力する。")
    raw = postprocess.strip_think(raw or "").strip()
    line = (raw.splitlines() or [""])[0]
    return line.strip("\"'「」『』 　。．、\n")[:30]


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
#  OCR API (VBA / Python など外部から呼ぶ用)
#    画像パス(または base64)+ 指示文 を受け取り、Vision モデルの応答を返す。
#    例) {"path": "C:/work/伝票.png", "instruction": "購入数量を数字だけで返信"}
# ============================================================
DEFAULT_OCR_INSTRUCTION = (
    "この画像に書かれている文字をすべて正確に読み取り、本文だけを出力してください。"
    "前置き・説明・注釈は不要です。"
)
_OCR_IMG_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}


class OcrBody(BaseModel):
    path: str = ""                  # サーバ上の画像ファイルパス
    image_b64: str = ""             # path の代わりに base64 / data URL を直接渡す場合
    instruction: str = ""           # 読み取り後の指示(空なら全文OCR)
    model: str = ""                 # 使用モデル(空なら設定のVisionモデル)
    num_predict: int = 512          # 応答の最大トークン
    temperature: float = 0.1


def _check_ocr_auth(x_api_key: Optional[str], rag_session: Optional[str]) -> None:
    """OCR API の認証。認証無効時は素通り。有効時は API キー か セッションCookie を要求。"""
    if not settings.auth_enabled:
        return
    if (settings.ocr_api_key and x_api_key
            and hmac.compare_digest(x_api_key, settings.ocr_api_key)):
        return
    if auth.is_authenticated(rag_session):
        return
    raise HTTPException(401, "認証が必要です(X-API-Key ヘッダ、またはログインセッションが必要)")


@app.post("/api/ocr")
def api_ocr(body: OcrBody,
            x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
            rag_session: Optional[str] = Cookie(default=None)) -> dict:
    _check_ocr_auth(x_api_key, rag_session)

    # --- 画像の取得(base64 優先、無ければパスから読込) ---
    if body.image_b64.strip():
        b64 = body.image_b64.strip()
        if b64.startswith("data:"):
            try:
                b64 = b64.split(",", 1)[1]
            except IndexError:
                raise HTTPException(400, "data URL の形式が不正です")
    elif body.path.strip():
        p = Path(body.path.strip())
        if not p.is_file():
            raise HTTPException(404, f"ファイルが見つかりません: {p}")
        if p.suffix.lower() not in _OCR_IMG_SUFFIXES:
            raise HTTPException(400, f"対応していない画像形式です: {p.suffix}")
        data = p.read_bytes()
        if len(data) > settings.max_upload_mb * 1024 * 1024:
            raise HTTPException(413, f"画像が大きすぎます(上限 {settings.max_upload_mb}MB)")
        b64 = base64.b64encode(data).decode("ascii")
    else:
        raise HTTPException(400, "path か image_b64 のどちらかを指定してください")

    # --- モデル確認 ---
    model = llm.resolve_installed((body.model or settings.vision_model or "").strip())
    if not model:
        raise HTTPException(400, "Vision モデルが未設定です(model 指定か VISION_MODEL 設定が必要)")
    if not llm.is_ollama_available():
        raise HTTPException(503, f"Ollama に接続できません({settings.ollama_host})")
    if not llm.is_model_installed(model):
        raise HTTPException(400, f"モデル『{model}』が見つかりません。`ollama pull {model}` を実行してください。")

    instruction = body.instruction.strip() or DEFAULT_OCR_INSTRUCTION
    log.info("OCR要求 [model=%s src=%s] instruction=%s",
             model, (body.path or "(base64)")[:80], instruction[:60])
    try:
        text = llm.vision_complete([b64], instruction, model,
                                   temperature=float(body.temperature),
                                   num_predict=int(body.num_predict))
    except Exception as e:
        msg = str(e)
        if "image input is not supported" in msg.lower():
            raise HTTPException(
                400,
                f"モデル『{model}』は画像入力に対応していません(Vision/mmproj 非対応)。"
                "qwen2.5vl など画像対応モデルを model に指定するか VISION_MODEL に設定してください。")
        log.exception("OCR失敗")
        raise HTTPException(500, f"OCR処理に失敗しました: {msg}")

    return {"ok": True, "model": model, "result": (text or "").strip()}


# ============================================================
#  会話
# ============================================================
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


@app.get("/api/conversations", dependencies=[Depends(auth.require_auth)])
def api_list_conversations(kind: Optional[str] = None) -> list:
    return db.list_conversations(kind=kind)


@app.post("/api/conversations", dependencies=[Depends(auth.require_auth)])
def api_create_conversation(body: ConvCreate) -> dict:
    d = get_defaults()
    kind = body.kind or "chat"
    conv = db.create_conversation(
        title=body.title or ("新しいコード" if kind == "code" else "新しい会話"),
        model=body.model or d["model"],
        system_prompt=body.system_prompt,
        settings_json=body.settings or {},
        active_indexes=body.active_indexes or [],
        kind=kind,
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
        # Code の作業フォルダは安全なフォルダのみ許可(OS/システム等は不可)
        ws = (fields["settings"] or {}).get("workspace")
        if ws:
            ok, reason = safety.check_workspace(ws)
            if not ok:
                raise HTTPException(400, f"このフォルダは作業フォルダに設定できません: {reason}")
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
    with _code_ctx_lock:
        _code_ctx.pop(cid, None)
        _code_running.discard(cid)
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
    attachments: list[str] = []     # 文書添付のファイル名
    images: list[str] = []          # 画像(base64 / data URL)
    mode: str = "send"              # send | regenerate


_IMG_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg",
            "image/gif": "gif", "image/webp": "webp", "image/bmp": "bmp"}


def _save_b64_image(raw: str) -> Optional[tuple[str, str]]:
    """data URL / base64 を保存し (保存ファイル名, 純base64) を返す。失敗時 None。"""
    ext = "png"
    b64 = raw.strip()
    if b64.startswith("data:"):
        try:
            header, b64 = b64.split(",", 1)
            mime = header[5:].split(";")[0].strip().lower()
            ext = _IMG_EXT.get(mime, "png")
        except ValueError:
            return None
    try:
        data = base64.b64decode(b64, validate=False)
    except Exception:
        return None
    if not data or len(data) > 16 * 1024 * 1024:   # 16MB 上限
        return None
    name = f"img_{uuid.uuid4().hex}.{ext}"
    (settings.upload_dir / name).write_bytes(data)
    return name, b64


@app.post("/api/conversations/{cid}/generate", dependencies=[Depends(auth.require_auth)])
def api_generate(cid: str, body: GenerateBody) -> Response:
    conv = db.get_conversation(cid)
    if not conv:
        raise HTTPException(404, "会話が見つかりません")

    eff = effective_for(conv)
    mode = body.mode

    # --- 対象クエリとユーザーメッセージの確定 ---
    user_msg = None
    image_b64s: list[str] = []
    content = ""                # 既定(再生成パスでは未代入のため。タイトル生成参照の保険)
    is_first_msg = False        # 初回送信のときだけ True(タイトル自動生成の対象)
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
        # 直近ユーザー発話に画像があれば再読込(再生成でも画像を見られるように)
        for a in (last_user.get("attachments") or []):
            if isinstance(a, dict) and a.get("type") == "image" and a.get("file"):
                try:
                    data = (settings.upload_dir / a["file"]).read_bytes()
                    image_b64s.append(base64.b64encode(data).decode("ascii"))
                except Exception:
                    pass
    else:
        content = (body.content or "").strip()
        if not content and not body.images:
            raise HTTPException(400, "メッセージが空です")
        # 画像を保存して添付に記録
        image_atts: list = []
        for raw in (body.images or [])[:6]:
            saved = _save_b64_image(raw)
            if saved:
                name, b64 = saved
                image_atts.append({"type": "image", "file": name})
                image_b64s.append(b64)
        attachments = list(body.attachments or []) + image_atts
        user_msg = db.add_message(cid, "user", content or "(画像)", attachments=attachments)
        query = content or "画像について"
        # 初回メッセージなら、まず即時に仮タイトルを設定(LLM要約は生成後に title イベントで反映)
        is_first_msg = conv.get("title") in (None, "", "新しい会話")
        if is_first_msg:
            fb = (content or "画像").strip().splitlines()[0][:30] or "新しい会話"
            db.update_conversation(cid, title=fb)

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
                                "score": round(h["score"], 3), "attachment": h["attachment"],
                                "text": (h.get("text") or "")[:1500]})   # クリックで原文(該当チャンク)表示

    history = db.list_messages(cid)
    # 参照フォルダ(インデックス)を選択している会話は、その資料だけで回答する厳格モード。
    strict_rag = bool(conv.get("active_indexes"))
    messages = llm.build_messages(eff["system_prompt"], history, hits, strict=strict_rag)
    use_vision = bool(image_b64s)
    # Vision/OCR モデルは設定(既定値)で選べる。未設定なら .env の VISION_MODEL を使用。
    vision_model = (eff.get("vision_model") or settings.vision_model or "").strip()
    model = llm.resolve_installed(vision_model) if use_vision else eff["model"]
    if use_vision and messages and messages[-1].get("role") == "user":
        messages[-1]["images"] = image_b64s
        if (messages[-1].get("content") or "").strip() in ("", "(画像)"):
            messages[-1]["content"] = "添付された画像の内容を読み取り、日本語で説明・回答してください。"

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
        if use_vision:
            if not vision_model:
                yield sse({"type": "error", "error": "画像を理解するにはVisionモデルが必要です。設定でVision/OCRモデルを選択してください。"})
                return
            if not llm.is_model_installed(vision_model):
                yield sse({"type": "error",
                           "error": f"Visionモデル『{vision_model}』が見つかりません。`ollama pull {vision_model}` を実行してください。"})
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
            asst = db.add_message(cid, "assistant", postprocess.clean(acc_content), sources=sources)
            saved = True
            log.info("生成完了 [conv=%s] %d文字", cid, len(acc_content))
            if is_first_msg:        # 初回はLLMで短いタイトルを生成して反映(回答後・体感遅延なし)
                try:
                    t = _make_title(content, eff["model"])
                    if t:
                        db.update_conversation(cid, title=t)
                        yield sse({"type": "title", "title": t})
                except Exception:
                    log.exception("自動タイトル生成に失敗(仮タイトルのまま)")
            yield sse({"type": "done", "message": asst})
        except GeneratorExit:
            # クライアント切断(停止ボタン)
            log.info("生成停止(クライアント切断)[conv=%s]", cid)
            raise
        except Exception as e:
            log.exception("生成エラー")
            emsg = str(e)
            low = emsg.lower()
            # 選択したモデルが画像入力(Vision)に対応していない場合の分かりやすい案内。
            if use_vision and "image input is not supported" in low:
                emsg = (f"選択中のモデル『{model}』は画像入力に対応していません"
                        "(Vision/mmproj 非対応)。設定の「画像認識モデル」で画像対応モデル"
                        "(例: qwen2.5vl / llama3.2-vision / gemma3)を選択してください。")
            # コンテキスト長を超えた場合の案内。
            elif "context" in low and ("exceed" in low or "ctx" in low or "context size" in low):
                emsg = ("コンテキスト長を超えました。チャット欄の『参照』件数を減らす(∞→5など)、"
                        "または設定でコンテキスト長(num_ctx)を上げてください。")
            yield sse({"type": "error", "error": emsg})
        finally:
            if not saved and acc_content.strip():
                db.add_message(cid, "assistant", postprocess.clean(acc_content), sources=sources)

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}
    return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)


# ============================================================
#  コードエージェント(Code タブ)
# ============================================================
# 会話ごとのエージェント文脈(ツール往復を含む)をメモリ保持。
# 再起動で消えるが、テキスト履歴はDBに残るため再構築できる。
_code_ctx: dict[str, list] = {}
_code_running: set[str] = set()
_code_ctx_lock = threading.Lock()


class AgentBody(BaseModel):
    content: str = ""


class ApproveBody(BaseModel):
    action_id: str
    approved: bool = False
    scope: Optional[str] = None   # "always" で以後このセッションの編集を自動適用
    reason: Optional[str] = None  # 拒否理由(任意。モデルにどう直すか伝える)


class AnswerBody(BaseModel):
    action_id: str
    answer: str = ""                       # 旧形式(単一回答)
    answers: Optional[list] = None         # 新形式(質問ごとの選択ラベル配列)


def _init_code_ctx(cid: str, ws: Path) -> list:
    """システム+作業フォルダ案内(+CLAUDE.md)+これまでのテキスト履歴から文脈を再構築。"""
    msgs: list = [
        {"role": "system", "content": agent.SYSTEM_PROMPT},
        {"role": "user", "content": f"作業フォルダの絶対パスは {ws} です。この中だけで作業してください。"},
    ]
    instructions = agent.read_project_instructions(ws)
    if instructions:
        msgs.append({"role": "user",
                     "content": "このプロジェクトの指示書(CLAUDE.md 等)です。従ってください:\n\n" + instructions})
    msgs.append({"role": "assistant", "content": "了解しました。依頼をどうぞ。"})
    for m in db.list_messages(cid):
        if m["role"] in ("user", "assistant"):
            msgs.append({"role": m["role"], "content": m["content"]})
    return msgs


@app.post("/api/conversations/{cid}/agent", dependencies=[Depends(auth.require_auth)])
def api_agent(cid: str, body: AgentBody) -> Response:
    conv = db.get_conversation(cid)
    if not conv:
        raise HTTPException(404, "会話が見つかりません")
    if conv.get("kind") != "code":
        raise HTTPException(400, "コード用の会話ではありません")

    s = conv.get("settings") or {}
    workspace = (s.get("workspace") or "").strip()
    allow_changes = bool(s.get("allow_changes"))
    plan_mode = bool(s.get("plan_mode", True))
    auto_accept_edits = bool(s.get("auto_accept_edits"))
    if not workspace:
        raise HTTPException(400, "作業フォルダが設定されていません。先にフォルダを選択してください。")
    ws = Path(workspace).expanduser()
    if not ws.is_dir():
        raise HTTPException(400, f"作業フォルダが存在しません: {workspace}")
    # 実行時にも再検証(設定後にフォルダが移動/変更された場合や、安全規則の更新に追従)
    ok, reason = safety.check_workspace(workspace)
    if not ok:
        raise HTTPException(400, f"このフォルダでは実行できません: {reason}")

    content = (body.content or "").strip()
    if not content:
        raise HTTPException(400, "依頼が空です")

    eff = effective_for(conv)
    model = eff["model"]
    num_ctx = int(eff["num_ctx"]) or None   # 0 はモデル既定。Chat と同じく設定値を反映

    # 文脈を用意(新規ならDB履歴から再構築) → 依頼をDBへ保存 → 文脈へ追加
    with _code_ctx_lock:
        if cid in _code_running:
            raise HTTPException(409, "この会話は別の処理を実行中です")
        ctx = _code_ctx.get(cid)
        if ctx is None:
            ctx = _init_code_ctx(cid, ws.resolve())
            _code_ctx[cid] = ctx
        _code_running.add(cid)

    user_msg = db.add_message(cid, "user", content)
    # 文脈が大きくなっていれば自動圧縮(古い履歴を要約に置換)してから依頼を追加
    try:
        if agent.compact_ctx_with_model(model, ctx, num_ctx):
            log.info("文脈を自動圧縮しました [conv=%s]", cid)
    except Exception:
        log.exception("文脈圧縮に失敗(無視して続行)")
    ctx.append({"role": "user", "content": content})
    if conv.get("title") in (None, "", "新しい会話", "新しいコード"):
        title = content.splitlines()[0][:30] if content.strip() else "コード"
        db.update_conversation(cid, title=title or "コード")

    def _finish():
        with _code_ctx_lock:
            _code_running.discard(cid)

    def gen():
        yield sse({"type": "user_saved", "message": user_msg})
        if not model:
            yield sse({"type": "error", "error": "モデルが選択されていません。"})
            _finish()
            return
        if not llm.is_ollama_available():
            yield sse({"type": "error",
                       "error": f"Ollama に接続できません({settings.ollama_host})。`ollama serve` を起動してください。"})
            _finish()
            return

        log.info("エージェント開始 [conv=%s model=%s ws=%s allow=%s plan=%s] 依頼=%s",
                 cid, model, ws, allow_changes, plan_mode, content[:60])
        acc_text: list[str] = []
        steps: list[dict] = []    # 再表示用にステップを保存(差分・計画・TODO含む)
        buf: list[str] = []       # ストリーミング中の本文バッファ

        def flush_text():
            if buf:
                tt = "".join(buf); buf.clear()
                if tt.strip():
                    steps.append({"type": "assistant", "text": tt})
                    acc_text.append(tt)

        try:
            for ev in agent.run_stream(model, ctx, str(ws.resolve()), allow_changes, plan_mode,
                                       num_ctx, auto_accept_edits=auto_accept_edits):
                t = ev.get("type")
                if t in ("assistant_delta", "assistant"):
                    if ev.get("text"):
                        buf.append(ev["text"])
                    yield sse(ev)
                    continue
                if t == "thinking":
                    yield sse(ev)   # 思考は表示のみ(本文に混ぜず・ステップにも保存しない)
                    continue
                flush_text()   # 区切り → それまでの本文を1ステップとして確定
                if t == "tool_call":
                    steps.append({"type": "tool_call", "name": ev.get("name"), "args": ev.get("args", {})})
                elif t == "tool_result":
                    s = {"type": "tool_result", "name": ev.get("name"),
                         "status": ev.get("status"), "result": ev.get("result", "")}
                    if ev.get("diff"):
                        s["diff"] = ev["diff"]
                    steps.append(s)
                elif t == "plan":
                    steps.append({"type": "plan", "plan": ev.get("plan", "")})
                elif t == "todos":
                    steps.append({"type": "todos", "todos": ev.get("todos", [])})
                elif t == "ask":
                    steps.append({"type": "ask", "context": ev.get("context", ""),
                                  "questions": ev.get("questions", [])})
                yield sse(ev)
        except GeneratorExit:
            log.info("エージェント停止(クライアント切断)[conv=%s]", cid)
            raise
        except Exception as e:
            log.exception("エージェントエラー")
            yield sse({"type": "error", "error": str(e)})
        finally:
            flush_text()
            text = "\n\n".join(x for x in acc_text if x).strip() or "(操作を実行しました)"
            db.add_message(cid, "assistant", postprocess.clean(text), sources=steps)   # チャットと同じ後処理
            _finish()

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}
    return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)


@app.post("/api/code/approve", dependencies=[Depends(auth.require_auth)])
def api_code_approve(body: ApproveBody) -> dict:
    ok = agent.resolve(body.action_id, body.approved, body.scope, body.reason)
    return {"ok": ok}


@app.post("/api/code/answer", dependencies=[Depends(auth.require_auth)])
def api_code_answer(body: AnswerBody) -> dict:
    ans = body.answers if body.answers is not None else body.answer
    ok = agent.resolve_answer(body.action_id, ans)
    return {"ok": ok}


class UndoBody(BaseModel):
    undo_id: str


@app.post("/api/code/undo", dependencies=[Depends(auth.require_auth)])
def api_code_undo(body: UndoBody) -> dict:
    """適用済みのファイル変更を取り消す(復元/新規は削除)。"""
    msg = agent.undo(body.undo_id)
    return {"ok": not msg.startswith("[エラー]"), "message": msg}


@app.get("/api/conversations/{cid}/file", dependencies=[Depends(auth.require_auth)])
def api_code_file(cid: str, path: str) -> dict:
    """Code 会話の作業フォルダ内のファイルを安全に読み出して返す(本文の path:line リンク閲覧用)。"""
    max_bytes = 2_000_000   # 閲覧上限(クライアントからは変更不可)
    conv = db.get_conversation(cid)
    if not conv:
        raise HTTPException(404, "会話が見つかりません")
    if conv.get("kind") != "code":
        raise HTTPException(400, "コード用の会話ではありません")
    s = conv.get("settings") or {}
    workspace = (s.get("workspace") or "").strip()
    if not workspace:
        raise HTTPException(400, "作業フォルダが設定されていません")
    ws = Path(workspace).expanduser()
    if not ws.is_dir():
        raise HTTPException(400, f"作業フォルダが存在しません: {workspace}")
    rel = re.sub(r":\d+$", "", (path or "").strip().replace("\\", "/"))  # 末尾の :行番号 は除去
    if not rel:
        raise HTTPException(400, "パスが空です")
    try:
        fp = agent._safe_path(ws.resolve(), rel)   # 作業フォルダ外・保護領域は ValueError
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not fp.is_file():
        raise HTTPException(404, f"ファイルが見つかりません: {rel}")
    try:
        size = fp.stat().st_size
    except OSError:
        raise HTTPException(404, "ファイルにアクセスできません")
    bin_ext = {".xlsx", ".xlsm", ".xls", ".docx", ".pptx", ".pdf", ".png", ".jpg",
               ".jpeg", ".gif", ".webp", ".zip", ".exe", ".dll", ".bin", ".so"}
    if fp.suffix.lower() in bin_ext:
        return {"path": rel, "binary": True, "size": size, "note": "バイナリ形式のため表示できません。"}
    if size > max_bytes:
        return {"path": rel, "too_large": True, "size": size,
                "note": f"ファイルが大きすぎます({size:,} バイト)。"}
    data = fp.read_bytes()
    if b"\x00" in data[:4096]:
        return {"path": rel, "binary": True, "size": size, "note": "バイナリ形式のため表示できません。"}
    text = data.decode("utf-8", errors="replace")
    return {"path": rel, "content": text, "size": size,
            "lines": text.count("\n") + 1, "lang": fp.suffix.lstrip(".").lower()}


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
    items = db.list_indexes()
    for it in items:
        st = db.get_kv(f"summary:{it['id']}") or {}
        it["summary"] = {
            "status": st.get("status", "none"),
            "msg": st.get("msg", ""),
            "has_result": bool(st.get("result")),
            "finished_at": st.get("finished_at"),
        }
        it["bg_threshold"] = SUMMARY_BG_THRESHOLD
    return items


@app.post("/api/indexes", dependencies=[Depends(auth.require_auth)])
def api_create_index(body: IndexCreate) -> dict:
    if not body.paths:
        raise HTTPException(400, "フォルダが指定されていません")
    # OS/システムやアプリのデータ領域(secret.key 等)を資料として取り込ませない
    for p in body.paths:
        if safety.is_within_protected(p):
            raise HTTPException(400, "OS・システムやアプリのデータ領域は資料に取り込めません")
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
    db.set_kv(f"summary:{iid}", {"status": "none"})   # 内容が変わるため古い要約は破棄
    _build_async(iid, idx["paths"])
    return db.get_index(iid)


class SummarizeBody(BaseModel):
    instruction: str = ""
    model: Optional[str] = None
    map_model: Optional[str] = None
    categories: list[str] = []


@app.post("/api/indexes/{iid}/summarize", dependencies=[Depends(auth.require_auth)])
def api_index_summarize(iid: str, body: SummarizeBody) -> Response:
    """資料フォルダ配下の全ファイルを map-reduce で一括要約(進捗をSSEで配信)。"""
    idx = db.get_index(iid)
    if not idx:
        raise HTTPException(404, "資料が見つかりません")
    files = rag.scan_files(idx.get("paths") or [])
    defs = get_defaults()
    model = body.model or defs["model"]
    map_model = (body.map_model or defs.get("summarize_map_model") or "").strip() or None
    if map_model == model:
        map_model = None
    instruction = (body.instruction or "").strip()
    categories = [c.strip() for c in (body.categories or []) if c and c.strip()]

    def gen():
        if not files:
            yield sse({"type": "error", "error": "対象ファイルがありません"})
            return
        if not model:
            yield sse({"type": "error", "error": "モデルが選択されていません"})
            return
        if not llm.is_ollama_available():
            yield sse({"type": "error",
                       "error": f"Ollama に接続できません({settings.ollama_host})。"})
            return
        yield sse({"type": "start", "files": len(files), "model": model, "map_model": map_model})
        log.info("一括要約 開始 [idx=%s files=%d model=%s map=%s] 観点=%s cats=%d",
                 iid, len(files), model, map_model, instruction[:40], len(categories))
        fn = summarize.model_summarize_fn(model, instruction, map_model=map_model, categories=categories)
        try:
            for ev in summarize.stream_summarize(files, instruction, fn):
                yield sse(ev)
        except GeneratorExit:
            log.info("一括要約 停止(クライアント切断)[idx=%s]", iid)
            raise
        except Exception as e:
            log.exception("一括要約エラー")
            yield sse({"type": "error", "error": str(e)})

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}
    return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)


# --- バックグラウンド一括要約(参照ファイルが多いとき。ウィンドウを出さず裏で実行) ---
SUMMARY_BG_THRESHOLD = 100   # この件数以上はフロントが裏(バックグラウンド)実行を選ぶ
_summary_cancel: set[str] = set()
_summary_lock = threading.Lock()


def _summary_set(iid: str, **fields) -> dict:
    st = db.get_kv(f"summary:{iid}", {}) or {}
    st.update(fields)
    db.set_kv(f"summary:{iid}", st)
    return st


def _summarize_worker(iid: str, files, instruction: str, categories: list,
                      model: str, map_model) -> None:
    db.set_kv(f"summary:{iid}", {
        "status": "running", "files": len(files), "msg": "準備中…",
        "instruction": instruction, "categories": categories, "map_model": map_model,
        "result": None, "error": None, "started_at": time.time(), "finished_at": None})
    log.info("一括要約(裏) 開始 [idx=%s files=%d model=%s map=%s]", iid, len(files), model, map_model)
    fn = summarize.model_summarize_fn(model, instruction, map_model=map_model, categories=categories)
    gen = summarize.stream_summarize(files, instruction, fn)
    try:
        for ev in gen:
            with _summary_lock:
                canceled = iid in _summary_cancel
            if canceled:
                gen.close()
                _summary_set(iid, status="canceled", msg="中止しました", finished_at=time.time())
                log.info("一括要約(裏) 中止 [idx=%s]", iid)
                break
            t = ev.get("type")
            if t == "progress":
                _summary_set(iid, msg=ev.get("msg", ""))
            elif t == "result":
                _summary_set(iid, status="done", result=ev.get("text", ""),
                             msg="完了", finished_at=time.time())
                log.info("一括要約(裏) 完了 [idx=%s]", iid)
            elif t == "error":
                _summary_set(iid, status="error", error=ev.get("error", ""),
                             msg="エラー", finished_at=time.time())
    except Exception as e:
        log.exception("一括要約(裏) 失敗 [idx=%s]", iid)
        _summary_set(iid, status="error", error=str(e), msg="エラー", finished_at=time.time())
    finally:
        with _summary_lock:
            _summary_cancel.discard(iid)


@app.post("/api/indexes/{iid}/summarize/start", dependencies=[Depends(auth.require_auth)])
def api_summarize_start(iid: str, body: SummarizeBody) -> dict:
    """裏(バックグラウンド)で一括要約を開始する。進捗は GET /summary でポーリング。"""
    idx = db.get_index(iid)
    if not idx:
        raise HTTPException(404, "資料が見つかりません")
    cur = db.get_kv(f"summary:{iid}")
    if cur and cur.get("status") == "running":
        return {"status": "running", "files": cur.get("files")}
    files = rag.scan_files(idx.get("paths") or [])
    if not files:
        raise HTTPException(400, "対象ファイルがありません")
    if not llm.is_ollama_available():
        raise HTTPException(503, f"Ollama に接続できません({settings.ollama_host})。")
    defs = get_defaults()
    model = body.model or defs["model"]
    map_model = (body.map_model or defs.get("summarize_map_model") or "").strip() or None
    if map_model == model:
        map_model = None
    instruction = (body.instruction or "").strip()
    categories = [c.strip() for c in (body.categories or []) if c and c.strip()]
    with _summary_lock:
        _summary_cancel.discard(iid)
    threading.Thread(target=_summarize_worker,
                     args=(iid, files, instruction, categories, model, map_model),
                     daemon=True).start()
    return {"status": "running", "files": len(files)}


@app.get("/api/indexes/{iid}/summary", dependencies=[Depends(auth.require_auth)])
def api_summary_status(iid: str) -> dict:
    return db.get_kv(f"summary:{iid}", {"status": "none"}) or {"status": "none"}


@app.post("/api/indexes/{iid}/summary/cancel", dependencies=[Depends(auth.require_auth)])
def api_summary_cancel(iid: str) -> dict:
    with _summary_lock:
        _summary_cancel.add(iid)
    return {"ok": True}


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
    format: str = "md"        # md|txt|html|pdf|docx|xlsx|csv|pptx|code
    ext: Optional[str] = None  # format=code のときの拡張子(例: bas)
    title: Optional[str] = "回答"
    images: Optional[list] = None   # PDF用: Mermaid図のPNG(順序対応) [{data(base64), w, h}]


def _safe_stem(title: str) -> str:
    stem = re.sub(r"[\\/:*?\"<>|\n\r\t]", "", (title or "回答")).strip()
    return (stem[:40] or "回答")


@app.post("/api/export", dependencies=[Depends(auth.require_auth)])
def api_export(body: ExportBody) -> Response:
    try:
        data, mime, ext = export.export_content(body.content, body.format, body.ext,
                                                body.title or "回答", images=body.images)
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
@app.get("/api/uploads/{name}", dependencies=[Depends(auth.require_auth)])
def api_upload_file(name: str) -> FileResponse:
    safe = (settings.upload_dir / name).resolve()
    if settings.upload_dir.resolve() not in safe.parents or not safe.is_file():
        raise HTTPException(404, "ファイルが見つかりません")
    return FileResponse(str(safe))


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "ts": time.time()}
