"""app.services.index_service の単体テスト(重い依存なし)。

一時 DB に切り替え、列挙の付帯情報・作成時の保護領域拒否・要約パラメータ解決・
要約状態/キャンセルの純粋ロジックを検証する。rag のビルドや summarize / ollama は
スタブ化して呼ばない。pytest でも `python tests/test_index_service.py` でも動く。
"""
import contextlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db                       # noqa: E402
from app.services import index_service   # noqa: E402


@contextlib.contextmanager
def temp_db():
    """settings.db_path を一時ファイルへ差し替え、初期化して使う。終了時に復元。"""
    d = tempfile.mkdtemp(prefix="idxsvc_test_")
    old = db.settings.db_path
    db.settings.db_path = Path(d) / "t.db"
    try:
        db.init_db()
        yield
    finally:
        db.settings.db_path = old
        shutil.rmtree(d, ignore_errors=True)


@contextlib.contextmanager
def patched(obj, name, value):
    """obj.name を一時的に value に差し替える(終了時に復元)。"""
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


# ---------------- list_indexes(付帯情報の付与)----------------
def test_list_indexes_enriches_summary_and_threshold():
    with temp_db():
        idx = db.create_index("資料A", ["/tmp/x"])
        db.set_kv(f"summary:{idx['id']}",
                  {"status": "done", "msg": "完了", "result": "本文", "finished_at": 123})
        it = next(i for i in index_service.list_indexes() if i["id"] == idx["id"])
        assert it["summary"]["status"] == "done"
        assert it["summary"]["has_result"] is True
        assert it["summary"]["msg"] == "完了"
        assert it["summary"]["finished_at"] == 123
        assert it["bg_threshold"] == index_service.SUMMARY_BG_THRESHOLD


def test_list_indexes_defaults_when_no_summary():
    with temp_db():
        idx = db.create_index("資料B", ["/tmp/y"])
        it = next(i for i in index_service.list_indexes() if i["id"] == idx["id"])
        assert it["summary"]["status"] == "none"
        assert it["summary"]["has_result"] is False


# ---------------- create_index ----------------
def test_create_index_derives_name_and_starts_build():
    started = {}
    with temp_db(), \
            patched(index_service, "build_async",
                    lambda iid, paths: started.update(iid=iid, paths=paths)), \
            patched(index_service.safety, "is_within_protected", lambda p: False):
        idx = index_service.create_index(None, ["/data/docs/規程"])
        assert idx["name"] == "規程"               # paths[0] の basename を採用
        assert started["iid"] == idx["id"]         # 構築が起動された
        assert db.get_index(idx["id"]) is not None


def test_create_index_rejects_protected_path():
    with temp_db(), \
            patched(index_service, "build_async", lambda *a, **k: None), \
            patched(index_service.safety, "is_within_protected", lambda p: True):
        try:
            index_service.create_index("x", ["/etc"])
            assert False, "保護領域は ValueError になるべき"
        except ValueError:
            pass


# ---------------- rebuild_index ----------------
def test_rebuild_missing_returns_none():
    with temp_db():
        assert index_service.rebuild_index("nope") is None


def test_rebuild_resets_summary_and_starts_build():
    started = {}
    with temp_db(), \
            patched(index_service, "build_async",
                    lambda iid, paths: started.update(iid=iid)):
        idx = db.create_index("R", ["/data/docs"])
        db.set_kv(f"summary:{idx['id']}", {"status": "done", "result": "x"})
        out = index_service.rebuild_index(idx["id"])
        assert out is not None
        assert db.get_kv(f"summary:{idx['id']}")["status"] == "none"   # 古い要約は破棄
        assert started["iid"] == idx["id"]


# ---------------- delete_index ----------------
def test_delete_missing_returns_false():
    with temp_db(), patched(index_service.rag, "delete_index_collection", lambda iid: None):
        assert index_service.delete_index("missing") is False


def test_delete_existing_returns_true():
    with temp_db(), patched(index_service.rag, "delete_index_collection", lambda iid: None):
        idx = db.create_index("D", ["/data/docs"])
        assert index_service.delete_index(idx["id"]) is True
        assert db.get_index(idx["id"]) is None


# ---------------- _resolve_summary_params ----------------
def test_resolve_summary_params_strips_and_filters():
    with patched(index_service, "get_defaults",
                 lambda: {"model": "main", "summarize_map_model": "small"}):
        model, map_model, instruction, cats = index_service._resolve_summary_params(
            None, None, "  観点  ", ["a", " ", "b ", ""])
        assert model == "main"
        assert map_model == "small"
        assert instruction == "観点"
        assert cats == ["a", "b"]


def test_resolve_summary_params_drops_map_equal_model():
    with patched(index_service, "get_defaults", lambda: {"model": "main"}):
        _, map_model, _, _ = index_service._resolve_summary_params("main", "main", "", [])
        assert map_model is None


# ---------------- summary_status / cancel ----------------
def test_summary_status_default_none():
    with temp_db():
        assert index_service.summary_status("unknown") == {"status": "none"}


def test_request_cancel_marks_id():
    index_service.request_cancel("idX")
    try:
        assert "idX" in index_service._summary_cancel
    finally:
        index_service._summary_cancel.discard("idX")   # 後始末


# ---------------- build_async の並行ガード(同一indexの二重ビルド防止)----------------
def test_build_guard_blocks_concurrent_same_index():
    assert index_service._acquire_build("idA") is True
    assert index_service._acquire_build("idA") is False   # 構築中は二重起動をスキップ
    assert index_service._acquire_build("idB") is True     # 別indexは並行OK
    index_service._release_build("idA")
    assert index_service._acquire_build("idA") is True      # 解放後は再取得できる
    index_service._release_build("idA")
    index_service._release_build("idB")


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
