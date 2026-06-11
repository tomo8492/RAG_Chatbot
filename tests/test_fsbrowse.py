"""共有サーバフォルダ対応(SHARED_FOLDERS / fsbrowse / build_index ガード)のテスト。

重い依存(chromadb の実体・埋め込みモデル)は使わない。pytest でも
`python tests/test_fsbrowse.py` でも動く。
"""
import contextlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, fsbrowse, rag          # noqa: E402
from app.config import Settings, settings  # noqa: E402


@contextlib.contextmanager
def temp_db():
    """settings.db_path を一時ファイルへ差し替え、初期化して使う。終了時に復元。"""
    d = tempfile.mkdtemp(prefix="fsbrowse_test_")
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


# ---------------- 設定のパース(SHARED_FOLDERS)----------------
def test_parse_paths_splits_on_semicolon_and_newline():
    raw = r"\\fileserver\共有\設計資料; /mnt/share/docs" + "\n" + r"\\nas\docs"
    assert Settings._parse_paths(raw) == [
        r"\\fileserver\共有\設計資料", "/mnt/share/docs", r"\\nas\docs"]


def test_parse_paths_strips_quotes_dedupes_and_drops_empty():
    raw = '"\\\\srv\\docs";;\\\\srv\\docs; '
    assert Settings._parse_paths(raw) == ["\\\\srv\\docs"]
    assert Settings._parse_paths("") == []
    assert Settings._parse_paths(None) == []


def test_parse_paths_keeps_windows_drive_colon_and_comma():
    # Windows パスは : や , を含み得るため、; 以外では分割しない
    raw = r"C:\Users\共有, 営業;D:\docs"
    assert Settings._parse_paths(raw) == [r"C:\Users\共有, 営業", r"D:\docs"]


# ---------------- get_roots(クイックアクセス)----------------
def test_get_roots_includes_shared_folders_without_existence_check():
    shares = [r"\\fileserver\共有\設計資料", "/mnt/no_such_share/docs"]
    with patched(settings, "shared_folders", shares):
        roots = fsbrowse.get_roots()
    names = [r["name"] for r in roots]
    paths = [r["path"] for r in roots]
    # 存在しない共有でも(切断中でも)候補に出る
    assert "共有: 設計資料" in names
    assert "共有: docs" in names
    assert shares[0] in paths and shares[1] in paths
    assert names[0] == "ホーム"   # 先頭はホームのまま


def test_get_roots_empty_shared_folders_keeps_default_roots():
    with patched(settings, "shared_folders", []):
        roots = fsbrowse.get_roots()
    assert roots[0]["name"] == "ホーム"
    assert all(not r["name"].startswith("共有:") for r in roots)


def test_share_display_name_variants():
    assert fsbrowse._share_display_name(r"\\srv\share\設計資料") == "設計資料"
    assert fsbrowse._share_display_name(r"\\srv\share\営業\\") == "営業"
    assert fsbrowse._share_display_name("//srv/share") == "share"
    assert fsbrowse._share_display_name("/mnt/docs/") == "docs"


# ---------------- list_dir(ネットワークパスのエラーメッセージ)----------------
def test_list_dir_missing_unc_mentions_share_access():
    try:
        fsbrowse.list_dir(r"\\no-such-server-xyz\share\docs" if os.name == "nt"
                          else "//no-such-server-xyz/share/docs")
        assert False, "FileNotFoundError が出るはず"
    except FileNotFoundError as e:
        assert "共有フォルダにアクセスできません" in str(e)


def test_list_dir_missing_local_keeps_plain_message():
    try:
        fsbrowse.list_dir("/no/such/dir/xyz")
        assert False, "FileNotFoundError が出るはず"
    except FileNotFoundError as e:
        assert "フォルダが見つかりません" in str(e)
        assert "共有フォルダ" not in str(e)


# ---------------- Officeロックファイル(~$ / .~lock.)の除外 ----------------
def test_scan_files_skips_office_lock_files():
    d = Path(tempfile.mkdtemp(prefix="lock_test_"))
    try:
        (d / "~$手順書.xlsx").write_bytes(b"\x00" * 32)        # Excelのロックファイル(zipではない)
        (d / ".~lock.メモ.txt").write_text("x", encoding="utf-8")   # LibreOffice
        (d / "本物.txt").write_text("本文", encoding="utf-8")
        out = rag.scan_files([str(d)])
        assert [p.name for p in out] == ["本物.txt"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_count_supported_excludes_lock_files():
    d = Path(tempfile.mkdtemp(prefix="lock_cnt_"))
    try:
        (d / "~$a.xlsx").write_bytes(b"\x00" * 32)
        (d / "b.txt").write_text("x", encoding="utf-8")
        assert fsbrowse.count_supported_recursive([str(d)]) == (1, False)
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------------- ベクトルDB破損の即時中断 ----------------
def test_build_index_aborts_fast_on_chroma_corruption():
    from types import SimpleNamespace

    class FakeCol:
        def get(self, **kw):
            return {"ids": [], "metadatas": [], "embeddings": None}

        def add(self, **kw):
            raise Exception("Error in compaction: Error constructing hnsw segment reader: "
                            "Error loading hnsw index")

        def delete(self, **kw):
            pass

    class FakeEmb:
        def embed_query(self, t):
            return [0.1] * 8

        def embed_documents(self, ts):
            return [[0.1] * 8 for _ in ts]

    d = Path(tempfile.mkdtemp(prefix="corrupt_test_"))
    try:
        (d / "a.txt").write_text("これは本文です", encoding="utf-8")
        with temp_db():
            idx = db.create_index("壊れ資料", [str(d)])
            db.update_index(idx["id"], file_count=7, chunk_count=99)
            with patched(rag, "get_embedder", lambda: FakeEmb()), \
                 patched(rag, "_collection", lambda name: FakeCol()), \
                 patched(rag, "_get_client",
                         lambda: SimpleNamespace(delete_collection=lambda n: None)):
                row = rag.build_index(idx["id"], [str(d)])
        assert row["status"] == "error"
        err = row["error"] or ""
        assert "削除" in err and "DATA_DIR" in err          # 復旧手順を案内
        assert row["file_count"] == 7 and row["chunk_count"] == 99   # 既存値は保持
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_is_corruption_error_detector():
    assert rag._is_corruption_error("Error loading hnsw index")
    assert rag._is_corruption_error("Error in compaction: x")
    assert not rag._is_corruption_error("connection refused")


# ---------------- build_index(切断中の共有を誤って「削除」しない)----------------
def test_build_index_aborts_when_path_unavailable_and_keeps_data():
    with temp_db():
        idx = db.create_index("共有資料", ["/no/such/share/docs"])
        db.update_index(idx["id"], status="ready", file_count=12, chunk_count=345)
        row = rag.build_index(idx["id"], ["/no/such/share/docs"])
        assert row["status"] == "error"
        assert "アクセスできません" in (row["error"] or "")
        # 既存の索引データ(件数)は触らない
        assert row["file_count"] == 12
        assert row["chunk_count"] == 345


def test_build_index_abort_reports_progress():
    msgs = []
    with temp_db():
        idx = db.create_index("共有資料", ["/no/such/share/docs"])
        rag.build_index(idx["id"], ["/no/such/share/docs"], progress=msgs.append)
    assert any("アクセスできません" in m for m in msgs)


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
