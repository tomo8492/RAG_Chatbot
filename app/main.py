"""
main.py
FastAPI 本体。API ルートと SSE ストリーミング生成。
"""
from __future__ import annotations

import base64
import ipaddress
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agent, auth, db, llm, postprocess, rag, safety
from .config import settings
from .defaults import effective_for
from .logging_setup import get_logger, set_request_id, setup_logging
from .routes import routers as _routers
from .sse import sse

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

# ドメイン別ルータ(routes/ 配下)を取り込む。main.py からの段階的分割の受け口。
for _r in _routers:
    app.include_router(_r)


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
        log.debug("_ip_allowed: 例外を無視して継続", exc_info=True)
        return host == "localhost"
    mapped = getattr(ip, "ipv4_mapped", None)   # ::ffff:192.168.x.x → 192.168.x.x(スマホ等の誤遮断を防ぐ)
    if mapped is not None:
        ip = mapped
    if ip.is_loopback or ip.is_private or ip.is_link_local:
        return True
    for net in settings.allowed_cidrs:  # .env の ALLOWED_CIDRS による追加許可(社内の非標準レンジ等)
        if ip.version == net.version and ip in net:
            return True
    return False


def _denied_page(host: str | None) -> str:
    """LAN制限で弾いたときの案内HTML(接続元IPと対処を表示。スマホでも読める)。"""
    ip = (host or "不明").replace("<", "").replace(">", "")
    hint = ""
    try:
        a = ipaddress.ip_address(host or "")
        a = getattr(a, "ipv4_mapped", None) or a
        if a.version == 4:
            hint = ".".join(str(a).split(".")[:3]) + ".0/24"
    except Exception:
        log.debug("_denied_page: 例外を無視して継続", exc_info=True)
        pass
    cidr = f"<li><code>.env</code> に <code>ALLOWED_CIDRS={hint}</code> を追加</li>" if hint else ""
    return ("<!doctype html><html lang=ja><head><meta charset=utf-8>"
            "<meta name=viewport content=\"width=device-width,initial-scale=1\">"
            "<title>アクセス制限</title><style>"
            "body{font-family:system-ui,-apple-system,sans-serif;max-width:560px;margin:36px auto;"
            "padding:0 22px;line-height:1.85;color:#1f2328}code{background:#eef0f3;padding:2px 6px;"
            "border-radius:5px;font-size:.9em}h1{font-size:20px}li{margin:.4em 0}</style></head><body>"
            "<h1>このネットワークからはアクセスできません</h1>"
            f"<p>社内ネットワーク限定(<code>LAN_ONLY</code>)が有効で、接続元IP <code>{ip}</code> が許可範囲外です。"
            "サーバ管理者は次のいずれかで許可できます。</p><ul>"
            f"{cidr}"
            "<li>または <code>LAN_ONLY=false</code>(社外に公開しない環境のみ)</li>"
            "<li>サーバ <code>HOST=0.0.0.0</code>・同一Wi-Fi接続・PCのファイアウォール(ポート開放)も確認</li>"
            "</ul></body></html>")


@app.middleware("http")
async def _lan_guard(request, call_next):
    if settings.lan_only:
        client = request.client
        host = client.host if client else None
        if not _ip_allowed(host):
            log.warning("LAN外からのアクセスを拒否: %s (UA=%s)",
                        host, request.headers.get("user-agent", "")[:80])
            return Response(_denied_page(host), status_code=403, media_type="text/html; charset=utf-8")
    return await call_next(request)


@app.middleware("http")
async def _observability(request, call_next):
    """リクエストID付与・アクセスログ・未処理例外のログ。

    失敗が必ず痕跡を残すようにし(サイレント握りつぶしを表に出す)、相関ID(req)で
    同時アクセス時でも1操作のログを追跡できるようにする。_lan_guard より外側で動く。
    """
    rid = uuid.uuid4().hex[:8]
    set_request_id(rid)
    t0 = time.time()
    try:
        resp = await call_next(request)
    except Exception:
        log.exception("未処理の例外: %s %s", request.method, request.url.path)
        resp = JSONResponse(
            {"error": "サーバ内部でエラーが発生しました", "request_id": rid},
            status_code=500,
        )
    resp.headers["X-Request-ID"] = rid
    log.info("%s %s -> %d (%dms)", request.method, request.url.path,
             resp.status_code, int((time.time() - t0) * 1000))
    return resp


# ============================================================
#  SSE ヘルパ
# ============================================================
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
#  会話 — 削除のみ(他の CRUD は routes/conversation_routes.py に分離。
#  削除は Code エージェント実行時状態の掃除を伴うため main.py に残置)
# ============================================================
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
            log.debug("_save_b64_image: 例外を無視して継続", exc_info=True)
            return None
    try:
        data = base64.b64decode(b64, validate=False)
    except Exception:
        log.debug("_save_b64_image: 例外を無視して継続", exc_info=True)
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
                    log.debug("api_generate: 例外を無視して継続", exc_info=True)
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

    # --- RAG 検索(追問は履歴をふまえ独立クエリへ書き換えてから検索) ---
    sources: list[dict] = []
    hits: list[dict] = []
    search_query = query
    try:
        prior = [m for m in db.list_messages(cid) if m["role"] in ("user", "assistant")]
        if prior and prior[-1]["role"] == "user":
            prior = prior[:-1]          # 今回の質問を除いた過去のやり取り
        search_query = llm.rewrite_query(prior, query, eff["model"])
        if search_query != query:
            log.info("クエリ書き換え [conv=%s] %s -> %s", cid, query[:40], search_query[:40])
    except Exception:
        log.exception("クエリ書き換えに失敗(原文で検索)")
    try:
        hits = rag.retrieve(search_query, conv.get("active_indexes", []), cid, int(eff["top_k"]))
        # 書き換えクエリで0件なら、元の質問でも検索して取りこぼしを防ぐ(書き換えの誤りに対する保険)。
        if not hits and search_query != query:
            hits = rag.retrieve(query, conv.get("active_indexes", []), cid, int(eff["top_k"]))
            if hits:
                log.info("書き換えで0件 → 原文で再検索しヒット [conv=%s]", cid)
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
    # 図を求めていない通常QAでは簡潔な整形ガイドにして根拠提示に集中させる。
    messages = llm.build_messages(eff["system_prompt"], history, hits,
                                  strict=strict_rag, diagram_hint=llm.wants_diagram(query),
                                  num_ctx=int(eff["num_ctx"]) or 0, num_predict=int(eff["num_predict"]))
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
        elif conv.get("active_indexes"):
            # 参照フォルダは選択済みだが関連箇所が無い場合は明示(strict-RAG の透明性)
            yield sse({"type": "sources", "sources": [],
                       "note": "参照資料の中に関連する箇所は見つかりませんでした"})

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
    images: list[str] = []          # スクショ等(base64/data URL)。Vision対応モデルで読む


def _resolve_mentions(ws: Path, content: str) -> str:
    """本文中の @相対パス を作業フォルダから読み、文脈の前置きにする(@file)。"""
    paths = re.findall(r"@([^\s,;:、。]+)", content or "")
    if not paths:
        return ""
    blocks, total = [], 0
    for rel in paths[:8]:
        try:
            p = agent._safe_path(ws, rel)
        except Exception:
            log.debug("_resolve_mentions: 例外を無視して継続", exc_info=True)
            continue
        if not p.is_file():
            continue
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")[:6000]
        except Exception:
            log.debug("_resolve_mentions: 例外を無視して継続", exc_info=True)
            continue
        blocks.append(f"【指定ファイル: {rel}】\n```\n{txt}\n```")
        total += len(txt)
        if total > 24000:
            break
    return ("\n\n".join(blocks) + "\n\n") if blocks else ""


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
    auto_verify = bool(s.get("auto_verify", True))      # 変更後にテスト等を自動実行して直す(既定ON)
    verify_cmd = (s.get("verify_cmd") or "").strip()    # 空=作業フォルダから自動検出
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
    if not content and not body.images:
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

    # スクショ等の画像を保存(Vision対応モデルで読む)
    image_atts: list = []
    image_b64s: list[str] = []
    for raw in (body.images or [])[:6]:
        saved = _save_b64_image(raw)
        if saved:
            name, b64 = saved
            image_atts.append({"type": "image", "file": name})
            image_b64s.append(b64)
    user_msg = db.add_message(cid, "user", content or "(画像)", attachments=image_atts)
    # 文脈が大きくなっていれば自動圧縮(古い履歴を要約に置換)してから依頼を追加
    try:
        if agent.compact_ctx_with_model(model, ctx, num_ctx):
            log.info("文脈を自動圧縮しました [conv=%s]", cid)
    except Exception:
        log.exception("文脈圧縮に失敗(無視して続行)")
    # @file 指定があれば対象ファイルを文脈に前置きしてから依頼を追加
    um: dict = {"role": "user", "content": _resolve_mentions(ws, content) + (content or "添付された画像について説明・対応してください")}
    if image_b64s:
        um["images"] = image_b64s
    ctx.append(um)
    if conv.get("title") in (None, "", "新しい会話", "新しいコード"):
        base = content or "画像"
        db.update_conversation(cid, title=(base.splitlines()[0][:30] or "コード"))

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
                                       num_ctx, auto_accept_edits=auto_accept_edits,
                                       auto_verify=auto_verify, verify_cmd=verify_cmd):
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
