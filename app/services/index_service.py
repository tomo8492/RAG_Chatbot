"""index_service.py
参照資料インデックス(ナレッジベース)と一括要約のビジネスロジック。

main.py のルートハンドラから切り出した「サービス層」。FastAPI/HTTP には依存せず、
既存の db / rag / summarize / llm をそのまま使う(ローカル LLM=Ollama 構成は維持)。
ルート層(routes/index_routes.py)は「検証 → 本モジュール呼び出し → 整形」に徹する。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Iterator, Optional

from .. import db, llm, rag, safety, summarize
from ..config import settings
from ..defaults import get_defaults
from ..logging_setup import get_logger
from ..sse import sse

log = get_logger("index_service")

# 参照ファイルがこの件数以上なら、フロントは裏(バックグラウンド)要約を選ぶ
SUMMARY_BG_THRESHOLD = 100

_summary_cancel: set[str] = set()
_summary_lock = threading.Lock()


# --------------------------------------------------------------------------
#  インデックス CRUD / 構築
# --------------------------------------------------------------------------
def build_async(iid: str, paths: list[str]) -> None:
    """インデックス構築をバックグラウンドスレッドで開始する。"""
    threading.Thread(target=rag.build_index, args=(iid, paths), daemon=True).start()


def list_indexes() -> list:
    """インデックス一覧に要約状況と裏実行しきい値を付与して返す。"""
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


def create_index(name: Optional[str], paths: list[str]) -> dict:
    """インデックスを作成し、構築を開始する。

    paths が空でないことは呼び出し側で検証済みである前提。
    保護領域(OS/システム/アプリのデータ領域)が含まれる場合は ValueError。
    """
    for p in paths:
        if safety.is_within_protected(p):
            raise ValueError("OS・システムやアプリのデータ領域は資料に取り込めません")
    name = name or (Path(paths[0]).name or paths[0])
    idx = db.create_index(name, paths)
    build_async(idx["id"], paths)
    log.info("インデックス作成開始: %s (%s)", name, paths)
    return idx


def rebuild_index(iid: str) -> Optional[dict]:
    """既存インデックスを再構築する。見つからなければ None。"""
    idx = db.get_index(iid)
    if not idx:
        return None
    db.update_index(iid, status="building", error=None)
    db.set_kv(f"summary:{iid}", {"status": "none"})   # 内容が変わるため古い要約は破棄
    build_async(iid, idx["paths"])
    return db.get_index(iid)


def delete_index(iid: str) -> bool:
    """インデックスとベクトルコレクションを削除する。見つからなければ False。"""
    if not db.get_index(iid):
        return False
    rag.delete_index_collection(iid)
    db.delete_index(iid)
    return True


# --------------------------------------------------------------------------
#  一括要約(map-reduce)
# --------------------------------------------------------------------------
def _resolve_summary_params(model_in, map_model_in, instruction_in, categories_in):
    """要約のモデル/補助モデル/観点/カテゴリを既定値とマージして正規化する。"""
    defs = get_defaults()
    model = model_in or defs["model"]
    map_model = (map_model_in or defs.get("summarize_map_model") or "").strip() or None
    if map_model == model:
        map_model = None
    instruction = (instruction_in or "").strip()
    categories = [c.strip() for c in (categories_in or []) if c and c.strip()]
    return model, map_model, instruction, categories


def iter_summary_sse(idx: dict, instruction_in: str, model_in, map_model_in,
                     categories_in) -> Iterator[str]:
    """同期(SSE)一括要約。SSE 文字列を逐次 yield する生成器を返す。"""
    iid = idx["id"]
    files = rag.scan_files(idx.get("paths") or [])
    model, map_model, instruction, categories = _resolve_summary_params(
        model_in, map_model_in, instruction_in, categories_in)

    def gen() -> Iterator[str]:
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
        fn = summarize.model_summarize_fn(model, instruction, map_model=map_model,
                                          categories=categories)
        try:
            for ev in summarize.stream_summarize(files, instruction, fn):
                yield sse(ev)
        except GeneratorExit:
            log.info("一括要約 停止(クライアント切断)[idx=%s]", iid)
            raise
        except Exception as e:
            log.exception("一括要約エラー")
            yield sse({"type": "error", "error": str(e)})

    return gen()


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


def start_summary_bg(idx: dict, instruction_in: str, model_in, map_model_in,
                     categories_in) -> dict:
    """裏(バックグラウンド)で一括要約を開始する。

    既に実行中ならその状態を返す。対象ファイルなしは ValueError、
    Ollama 不通は RuntimeError(呼び出し側で 400/503 に変換する)。
    """
    iid = idx["id"]
    cur = db.get_kv(f"summary:{iid}")
    if cur and cur.get("status") == "running":
        return {"status": "running", "files": cur.get("files")}
    files = rag.scan_files(idx.get("paths") or [])
    if not files:
        raise ValueError("対象ファイルがありません")
    if not llm.is_ollama_available():
        raise RuntimeError(f"Ollama に接続できません({settings.ollama_host})。")
    model, map_model, instruction, categories = _resolve_summary_params(
        model_in, map_model_in, instruction_in, categories_in)
    with _summary_lock:
        _summary_cancel.discard(iid)
    threading.Thread(target=_summarize_worker,
                     args=(iid, files, instruction, categories, model, map_model),
                     daemon=True).start()
    return {"status": "running", "files": len(files)}


def summary_status(iid: str) -> dict:
    return db.get_kv(f"summary:{iid}", {"status": "none"}) or {"status": "none"}


def request_cancel(iid: str) -> None:
    with _summary_lock:
        _summary_cancel.add(iid)
