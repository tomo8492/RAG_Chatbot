"""
rag.py
ChromaDB(永続)を使った RAG エンジン。
  - インデックス(ナレッジベース)= 1コレクション
  - 会話の添付ファイル = 会話ごとのコレクション
  - 複数コレクションを横断して検索し、上位を統合
"""
from __future__ import annotations

import shutil
import threading
import uuid
from pathlib import Path
from typing import Callable, Optional

from . import db
from .config import settings
from .embeddings import get_embedder
from .loaders import SUPPORTED_EXTS, load_file
from .logging_setup import get_logger
from .splitter import split_text

log = get_logger("rag")

_client = None
_client_lock = threading.Lock()
_BATCH = 128


def _get_client():
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                import chromadb
                from chromadb.config import Settings as ChromaSettings
                settings.chroma_dir.mkdir(parents=True, exist_ok=True)
                _client = chromadb.PersistentClient(
                    path=str(settings.chroma_dir),
                    settings=ChromaSettings(anonymized_telemetry=False, allow_reset=True),
                )
                log.info("ChromaDB 起動: %s", settings.chroma_dir)
    return _client


def _collection(name: str):
    return _get_client().get_or_create_collection(
        name=name, metadata={"hnsw:space": "cosine"}
    )


def _index_collection_name(iid: str) -> str:
    return f"idx_{iid}"


def _conv_collection_name(cid: str) -> str:
    return f"conv_{cid}"


# ============================================================
#  インデックス構築
# ============================================================
def scan_files(paths: list[str]) -> list[Path]:
    """対象フォルダ群から対応ファイルを再帰収集。"""
    found: list[Path] = []
    seen: set[str] = set()
    for p in paths:
        base = Path(p)
        if not base.exists():
            log.warning("パスが存在しません: %s", p)
            continue
        if base.is_file():
            candidates = [base]
        else:
            candidates = [f for f in base.rglob("*") if f.is_file()]
        for f in candidates:
            if f.suffix.lower() in SUPPORTED_EXTS:
                key = str(f.resolve())
                if key not in seen:
                    seen.add(key)
                    found.append(f)
    return found


def _embed_error_hint(e: Exception) -> str:
    """埋め込みモデルの読込失敗を、原因別の分かりやすい対処メッセージに変換。"""
    s = str(e)
    low = s.lower()
    if "certificate" in low or "ssl" in low or "self-signed" in low or "self signed" in low:
        return ("埋め込みモデルのダウンロードがSSL証明書エラーで失敗しました"
                "(社内プロキシのTLS検査が原因)。対処のいずれか: "
                "①【推奨】.env で EMBED_BACKEND=ollama にして `ollama pull nomic-embed-text` を実行 / "
                "② 環境変数 SSL_CERT_FILE に社内ルートCA証明書(.pem)を指定 / "
                "③ モデルを別PCで事前DLし、そのフォルダのパスを EMBED_MODEL に設定(オフライン)。")
    if ("client has been closed" in low or "offline" in low or "max retries" in low
            or "connection" in low or "timed out" in low or "failed to resolve" in low
            or "getaddrinfo" in low):
        return ("埋め込みモデルの取得に失敗しました(ネットワーク不通/オフライン)。対処: "
                "①【推奨】.env で EMBED_BACKEND=ollama にして `ollama pull nomic-embed-text` / "
                "② モデルを事前DLしてローカルパスを EMBED_MODEL に指定。")
    return f"埋め込みモデルの読み込みに失敗しました: {s}"


def build_index(iid: str, paths: list[str],
                progress: Optional[Callable[[str], None]] = None) -> dict:
    """フォルダ群を読み込み、コレクションを構築する。同期処理(ワーカースレッドから呼ぶ)。"""
    def emit(msg: str):
        log.info(msg)
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    try:
        from .defaults import chunk_params
        cs, co = chunk_params()
        embedder = get_embedder()
        cname = _index_collection_name(iid)
        client = _get_client()
        # 既存コレクションを作り直し(再インデックス対応)
        try:
            client.delete_collection(cname)
        except Exception:
            pass
        col = _collection(cname)

        emit("ファイルを走査中...")
        files = scan_files(paths)
        emit(f"対象ファイル: {len(files)} 件")
        if not files:
            db.update_index(iid, status="error", error="対応ファイルが見つかりません",
                            file_count=0, chunk_count=0)
            return db.get_index(iid)

        # 埋め込みモデルを先に1回ロード(失敗ならN件スキップせず、明確なエラーで中断する)
        try:
            emit("埋め込みモデルを準備中...(初回はDLに時間がかかります)")
            embedder.embed_query("ウォームアップ")
        except Exception as e:
            hint = _embed_error_hint(e)
            log.error("埋め込みモデルの準備に失敗: %s", e)
            db.update_index(iid, status="error", error=hint, file_count=0, chunk_count=0)
            emit("エラー: " + hint)
            return db.get_index(iid)

        total_chunks = 0
        ok_files = 0
        for fi, f in enumerate(files, 1):
            try:
                blocks = load_file(f)
                if not blocks:
                    emit(f"  [skip] {f.name}(抽出テキストなし)")
                    continue
                fpath = str(f.resolve())
                pending = []
                for b in blocks:
                    for chunk in split_text(b["text"], cs, co):
                        pending.append((chunk, b["source"], b["loc"], fpath))
                if not pending:
                    continue
                # バッチ単位で埋め込み&追加
                for s in range(0, len(pending), _BATCH):
                    batch = pending[s:s + _BATCH]
                    vecs = embedder.embed_documents([c[0] for c in batch])
                    col.add(
                        ids=[uuid.uuid4().hex for _ in batch],
                        embeddings=vecs,
                        documents=[c[0] for c in batch],
                        metadatas=[{"source": c[1], "loc": c[2], "path": c[3]} for c in batch],
                    )
                total_chunks += len(pending)
                ok_files += 1
                emit(f"  読込 {fi}/{len(files)}: {f.name}({len(pending)}チャンク)")
            except Exception as e:
                log.exception("ファイル読込失敗: %s", f)
                emit(f"  [skip] {f.name}(エラー: {e})")

        if total_chunks == 0:
            db.update_index(iid, status="error",
                            error="テキストを抽出できませんでした(スキャンPDF等の可能性)",
                            file_count=ok_files, chunk_count=0)
            return db.get_index(iid)

        db.update_index(iid, status="ready", error=None,
                        file_count=ok_files, chunk_count=total_chunks)
        emit(f"インデックス完了: {ok_files}ファイル / {total_chunks}チャンク")
        return db.get_index(iid)
    except Exception as e:
        log.exception("インデックス構築失敗")
        db.update_index(iid, status="error", error=str(e))
        return db.get_index(iid)


def delete_index_collection(iid: str) -> None:
    try:
        _get_client().delete_collection(_index_collection_name(iid))
    except Exception:
        pass


# ============================================================
#  会話の添付ファイル
# ============================================================
def add_attachment(cid: str, file_path: Path, original_name: str) -> int:
    """添付ファイルを会話コレクションに追加。追加チャンク数を返す。"""
    from .defaults import chunk_params
    cs, co = chunk_params()
    embedder = get_embedder()
    col = _collection(_conv_collection_name(cid))
    blocks = load_file(file_path)
    if not blocks:
        return 0
    pending = []
    for b in blocks:
        for chunk in split_text(b["text"], cs, co):
            pending.append((chunk, original_name, b["loc"], original_name))
    if not pending:
        return 0
    for s in range(0, len(pending), _BATCH):
        batch = pending[s:s + _BATCH]
        vecs = embedder.embed_documents([c[0] for c in batch])
        col.add(
            ids=[uuid.uuid4().hex for _ in batch],
            embeddings=vecs,
            documents=[c[0] for c in batch],
            metadatas=[{"source": c[1], "loc": c[2], "path": c[3], "attachment": True} for c in batch],
        )
    return len(pending)


def delete_conv_collection(cid: str) -> None:
    try:
        _get_client().delete_collection(_conv_collection_name(cid))
    except Exception:
        pass


# ============================================================
#  検索
# ============================================================
UNLIMITED_TOP_K = 9999  # これ以上は「上限なし(全件取得)」とみなすセンチネル


def retrieve(query: str, index_ids: list[str], conversation_id: Optional[str] = None,
             top_k: int = 5) -> list[dict]:
    """有効インデックス + 会話添付を横断検索し、上位 top_k を返す。

    top_k <= 0 で参照なし、top_k >= UNLIMITED_TOP_K で上限なし(全件)。
    """
    if top_k <= 0:
        return []
    unlimited = top_k >= UNLIMITED_TOP_K

    names: list[str] = [_index_collection_name(i) for i in index_ids]
    if conversation_id:
        names.append(_conv_collection_name(conversation_id))

    client = _get_client()
    # list_collections() は chromadb のバージョンにより Collection か str を返す
    existing = set()
    for c in client.list_collections():
        existing.add(c if isinstance(c, str) else getattr(c, "name", None))
    target = [n for n in names if n in existing]
    if not target:
        return []

    embedder = get_embedder()
    qvec = embedder.embed_query(query)

    hits: list[dict] = []
    for name in target:
        try:
            col = client.get_collection(name)
            n = col.count()
            if n == 0:
                continue
            n_results = n if unlimited else min(top_k, n)
            res = col.query(query_embeddings=[qvec], n_results=n_results)
            docs = res.get("documents", [[]])[0]
            metas = res.get("metadatas", [[]])[0]
            dists = res.get("distances", [[]])[0]
            for doc, meta, dist in zip(docs, metas, dists):
                hits.append({
                    "text": doc,
                    "source": (meta or {}).get("source", "不明"),
                    "loc": (meta or {}).get("loc", ""),
                    "path": (meta or {}).get("path", ""),
                    "attachment": bool((meta or {}).get("attachment", False)),
                    "score": 1.0 - float(dist),  # cosine距離 -> 類似度
                    "distance": float(dist),
                })
        except Exception as e:
            es = str(e).lower()
            if any(k in es for k in ("hnsw", "compactor", "backfill", "segment")):
                log.warning("インデックスが壊れている可能性があります。参照資料を削除→再作成してください "
                            "(同期フォルダOneDrive配下だと破損しやすい): %s / %s", name, str(e)[:160])
            else:
                log.warning("コレクション検索失敗(%s): %s", name, str(e)[:200])

    hits.sort(key=lambda h: h["distance"])
    return hits if unlimited else hits[:top_k]


def reset_all() -> None:
    """全コレクション削除(デバッグ用)。"""
    try:
        shutil.rmtree(settings.chroma_dir, ignore_errors=True)
    except Exception:
        pass
