"""チャット機能のE2Eデバッグテスト。

実アプリ(FastAPI)を TestClient で起動し、チャットの主要経路を末端まで通す:
  資料作成(図入りExcel×3)→ インデックス構築 → 会話作成・資料有効化
  → ①曖昧な短い質問 → 選択式聞き返し(clarify)が出る/構造が保存される
  → ②直後の同じ質問 → 連続聞き返しはせず生成に進む(出典・本文・done)
  → ③focus_source 再送 → 出典が選択資料に絞られ、図のURLが付く
  → ④図URLの配信(/api/doc-images)が200で返る
  → ⑤参考図つきエクスポート(docx)に画像が同梱される

Ollama・埋め込みモデルはスタブ(ChromaDB・SQLite・抽出・分割・配信は実物)。
pytest でも `python tests/test_chat_e2e.py` でも動く。
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, llm, ocr, rag        # noqa: E402
from app import main as appmain          # noqa: E402
from app.config import settings          # noqa: E402


@contextlib.contextmanager
def patched(obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


@contextlib.contextmanager
def temp_env():
    """data/chroma/db を一時フォルダへ。LAN制限はTestClient用に無効化。"""
    d = Path(tempfile.mkdtemp(prefix="chat_e2e_")).resolve()
    (d / "data").mkdir()
    docs = d / "docs"
    docs.mkdir()
    old = (settings.data_dir, settings.chroma_dir, settings.db_path, settings.lan_only)
    settings.data_dir = d / "data"
    settings.chroma_dir = d / "data" / "chroma"
    settings.db_path = d / "data" / "t.db"
    settings.lan_only = False
    old_client = rag._client
    rag._client = None
    try:
        db.init_db()
        yield docs
    finally:
        rag._client = old_client
        (settings.data_dir, settings.chroma_dir, settings.db_path, settings.lan_only) = old
        shutil.rmtree(d, ignore_errors=True)


class FakeEmb:
    def embed_query(self, t):
        return [0.1] * 8

    def embed_documents(self, ts):
        return [[0.1] * 8 for _ in ts]


def _png_bytes(w: int = 240, h: int = 160) -> bytes:
    import random
    from PIL import Image
    raw = random.Random(9).randbytes(w * h * 3)
    buf = io.BytesIO()
    Image.frombytes("RGB", (w, h), raw).save(buf, format="PNG")
    return buf.getvalue()


def _make_xlsx(path: Path, png: Path, sheet: str, lines: list[str]):
    import openpyxl
    from openpyxl.drawing.image import Image as XLImage
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    for i, ln in enumerate(lines, 1):
        ws.cell(row=i, column=1, value=ln)
    ws.add_image(XLImage(str(png)), "C2")
    wb.save(str(path))


_LAST_LLM_MESSAGES: dict = {}   # フェイクLLMが受け取った messages を検査用に保持


def _fake_chat_stream(messages, model, **kw):
    _LAST_LLM_MESSAGES["m"] = messages
    yield {"type": "thinking", "text": "確認中…"}
    yield {"type": "content", "text": "日当は出張旅費規程Aに基づき "}
    yield {"type": "content", "text": "5,000円です。"}


def _events(resp) -> list[dict]:
    out = []
    for line in resp.text.splitlines():
        if line.startswith("data: "):
            out.append(json.loads(line[6:]))
    return out


def _setup_corpus(docs: Path) -> str:
    """図入りxlsx×3(内容は別物・日当に言及)で索引を構築し、index_id を返す。"""
    png = docs / "fig.png"
    png.write_bytes(_png_bytes())
    _make_xlsx(docs / "A規程.xlsx", png, "日当",
               ["国内出張の日当について定める。", "課長級の日当は一日五千円とし精算は月末に行う。"])
    _make_xlsx(docs / "B精算手引.xlsx", png, "精算",
               ["経費精算の手引きである。", "宿泊を伴う場合の日当の申請はシステムから行うこと。"])
    _make_xlsx(docs / "C就業規則.xlsx", png, "規則",
               ["就業規則の抜粋を記載する。", "出張中の勤務時間は日当の支給対象として扱う。"])
    idx = db.create_index("テスト資料", [str(docs)])
    row = rag.build_index(idx["id"], [str(docs)])
    assert row["status"] == "ready", row.get("error")
    return idx["id"]


# ---------------- E2E 本体 ----------------
def test_chat_end_to_end():
    from fastapi.testclient import TestClient
    with temp_env() as docs, \
         patched(rag, "get_embedder", lambda: FakeEmb()), \
         patched(ocr, "describe_image_png", lambda b: "出張時の日当に関する一覧表の図"), \
         patched(llm, "rewrite_query", lambda prior, q, m: q), \
         patched(llm, "is_ollama_available", lambda: True), \
         patched(llm, "chat_stream", _fake_chat_stream), \
         patched(appmain, "_make_title", lambda c, m: ""):
        iid = _setup_corpus(docs)
        client = TestClient(appmain.app)

        conv = client.post("/api/conversations", json={"kind": "chat"}).json()
        cid = conv["id"]
        r = client.patch(f"/api/conversations/{cid}", json={"active_indexes": [iid]})
        assert r.status_code == 200

        # ① 短い曖昧な質問 → 聞き返し(clarify)
        evs = _events(client.post(f"/api/conversations/{cid}/generate",
                                  json={"content": "日当は?"}))
        types = [e["type"] for e in evs]
        assert "clarify" in types and types[-1] == "done", types
        cl = next(e for e in evs if e["type"] == "clarify")
        assert len(cl["options"]) >= 3 and cl["query"] == "日当は?"
        done = evs[-1]
        assert done["message"]["sources"][0]["clarify"]["query"] == "日当は?"   # 構造が保存される
        assert done["message"]["content"].startswith("🔎 確認: ")

        # ② 直後の同じ質問 → 連続では聞き返さず、生成まで進む
        evs2 = _events(client.post(f"/api/conversations/{cid}/generate",
                                   json={"content": "日当は?"}))
        types2 = [e["type"] for e in evs2]
        assert "clarify" not in types2
        assert "sources" in types2 and "content" in types2 and types2[-1] == "done"

        # ③ focus_source 再送 → 出典が選択資料に絞られ、図URLが付く
        evs3 = _events(client.post(f"/api/conversations/{cid}/generate",
                                   json={"content": "日当は?(対象資料: A規程.xlsx)",
                                         "focus_source": "A規程.xlsx",
                                         "skip_clarify": True}))
        src_ev = next(e for e in evs3 if e["type"] == "sources")
        assert src_ev["sources"], "出典があるはず"
        assert all(s["source"] == "A規程.xlsx" for s in src_ev["sources"])
        img_urls = [u for s in src_ev["sources"] for u in (s.get("images") or [])]
        assert img_urls and all(u.startswith("/api/doc-images/") for u in img_urls)
        body = "".join(e.get("delta", "") for e in evs3 if e["type"] == "content")
        assert "5,000円" in body
        assert evs3[-1]["type"] == "done"
        # 図の番号表(図N → 出典)がモデルへ提示される(本文「図N」直下への差し込み用)
        last_user = next(m for m in reversed(_LAST_LLM_MESSAGES["m"]) if m.get("role") == "user")
        assert "【利用できる図】" in last_user["content"]
        assert "図1: A規程.xlsx" in last_user["content"]

        # ④ 図の配信API
        rimg = client.get(img_urls[0])
        assert rimg.status_code == 200
        assert rimg.content[:8] == b"\x89PNG\r\n\x1a\n" or rimg.content[:3] == b"\xff\xd8\xff"

        # ⑤ 参考図つきエクスポート(docx)
        import base64
        rexp = client.post("/api/export", json={
            "content": "# 回答\n日当は5,000円です。", "format": "docx", "title": "回答",
            "figures": [{"data": base64.b64encode(rimg.content).decode(),
                         "caption": "A規程.xlsx シート:日当"}]})
        assert rexp.status_code == 200 and rexp.content[:2] == b"PK"
        with zipfile.ZipFile(io.BytesIO(rexp.content)) as z:
            assert any(n.startswith("word/media/") for n in z.namelist())

        # 会話履歴の再取得(リロード相当): clarify構造が残っている
        conv2 = client.get(f"/api/conversations/{cid}").json()
        msgs = conv2["messages"]
        clar_msgs = [m for m in msgs if m["role"] == "assistant"
                     and m.get("sources") and isinstance(m["sources"][0], dict)
                     and m["sources"][0].get("clarify")]
        assert clar_msgs, "聞き返しメッセージの構造が履歴に保存されている"


def test_static_assets_served_with_no_cache():
    from fastapi.testclient import TestClient
    with temp_env():
        client = TestClient(appmain.app)
        r = client.get("/")
        assert r.status_code == 200
        assert r.headers.get("cache-control") == "no-cache"     # 旧JSキャッシュの根治
        r2 = client.get("/static/js/chat.js")
        assert r2.status_code == 200
        assert r2.headers.get("cache-control") == "no-cache"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            passed += 1
        except AssertionError as e:
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"ERROR {fn.__name__}: {e!r}")
    print(f"{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
