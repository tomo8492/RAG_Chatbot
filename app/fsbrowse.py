"""
fsbrowse.py
サーバのファイルシステムを閲覧してフォルダを選ぶための機能。
(参照資料はサーバ=このアプリを動かすPC上にある想定)
"""
from __future__ import annotations

import os
import string
import time
from pathlib import Path

from . import safety
from .config import settings
from .loaders import SUPPORTED_EXTS
from .logging_setup import get_logger

log = get_logger("fsbrowse")


def _is_network_path(path) -> bool:
    """UNC(\\\\server\\share)/スラッシュ2つ始まりのネットワークパスか。"""
    s = str(path or "")
    return s.startswith("\\\\") or s.startswith("//")


def _share_display_name(path: str) -> str:
    """共有フォルダの表示名(末尾のフォルダ名。UNCルートなら共有名)。"""
    s = (path or "").replace("\\", "/").rstrip("/")
    return s.rsplit("/", 1)[-1] or path


def get_roots() -> list[dict]:
    """クイックアクセス用のルート(ホーム・共有サーバフォルダ・ドライブ等)。"""
    roots = []
    home = Path.home()
    roots.append({"name": "ホーム", "path": str(home)})
    # 登録済みの共有サーバフォルダ(.env の SHARED_FOLDERS)。
    # ネットワーク切断時に一覧表示まで固まらないよう、ここでは存在確認しない
    # (開いたときに list_dir が分かりやすいエラーを返す)。
    for sp in settings.shared_folders:
        roots.append({"name": "共有: " + _share_display_name(sp), "path": sp})
    if os.name == "nt":
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            if os.path.exists(drive):
                roots.append({"name": drive, "path": drive})
    else:
        roots.append({"name": "/", "path": "/"})
        # よくあるマウント先
        for cand in ("/mnt", "/media", "/Volumes"):
            if os.path.isdir(cand):
                roots.append({"name": cand, "path": cand})
    return roots


def _count_supported(dir_path: Path) -> int:
    """直下(非再帰)の対応ファイル数を素早く数える。"""
    n = 0
    try:
        with os.scandir(dir_path) as it:
            for e in it:
                try:
                    if e.is_file() and Path(e.name).suffix.lower() in SUPPORTED_EXTS:
                        n += 1
                except OSError:
                    log.debug("_count_supported: 例外を無視して継続", exc_info=True)
                    continue
    except (PermissionError, OSError):
        log.debug("_count_supported: 例外を無視して継続", exc_info=True)
        return 0
    return n


def list_dir(path: str | None) -> dict:
    """指定ディレクトリの中身(サブフォルダ + 直下の対応ファイル)を返す。"""
    if not path:
        path = str(Path.home())
    p = Path(path).expanduser()
    try:
        p = p.resolve()
    except OSError:
        log.debug("list_dir: 例外を無視して継続", exc_info=True)
        pass

    if not p.exists() or not p.is_dir():
        if _is_network_path(path):
            raise FileNotFoundError(
                f"共有フォルダにアクセスできません: {path}"
                "(ネットワーク接続と、サーバを起動しているユーザーのアクセス権を確認してください)")
        raise FileNotFoundError(f"フォルダが見つかりません: {path}")

    dirs: list[dict] = []
    files: list[dict] = []
    try:
        with os.scandir(p) as it:
            for e in it:
                if e.name.startswith("."):
                    continue
                try:
                    if e.is_dir():
                        dirs.append({"name": e.name, "path": str(Path(e.path))})
                    elif e.is_file() and Path(e.name).suffix.lower() in SUPPORTED_EXTS:
                        files.append({"name": e.name, "path": str(Path(e.path))})
                except OSError:
                    log.debug("list_dir: 例外を無視して継続", exc_info=True)
                    continue
    except PermissionError:
        raise PermissionError(f"アクセス権がありません: {path}")

    dirs.sort(key=lambda d: d["name"].lower())
    files.sort(key=lambda f: f["name"].lower())

    parent = str(p.parent) if p.parent != p else None
    ws_ok, ws_reason = safety.check_workspace(str(p))
    return {
        "path": str(p),
        "parent": parent,
        "dirs": dirs,
        "files": files,
        "supported_here": len(files),
        # Code の作業フォルダに選べるか(安全管理)。RAG資料の選択には影響しない。
        "workspace_ok": ws_ok,
        "workspace_reason": ws_reason,
    }


def count_supported_recursive(paths: list[str], max_files: int = 5000,
                              time_budget: float = 2.0) -> tuple[int, bool]:
    """
    選択フォルダ群の対応ファイル数(再帰・見積り用)。
    巨大ツリーや権限エラーで固まらないよう、件数・時間で打ち切る。
    戻り値: (件数, 打ち切ったか)
    """
    total = 0
    start = time.monotonic()
    for path in paths:
        base = Path(path)
        if not base.exists():
            continue
        if base.is_file():
            if base.suffix.lower() in SUPPORTED_EXTS:
                total += 1
            continue
        # os.walk は onerror で権限エラーを握りつぶせる(rglob と違い途中で例外を投げない)
        for _root, _dirs, filenames in os.walk(base, onerror=lambda e: None):
            for fn in filenames:
                if os.path.splitext(fn)[1].lower() in SUPPORTED_EXTS:
                    total += 1
                    if total >= max_files:
                        return total, True
            if time.monotonic() - start > time_budget:
                return total, True
    return total, False
