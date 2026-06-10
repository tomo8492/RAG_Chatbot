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
