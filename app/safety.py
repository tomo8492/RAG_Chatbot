"""
safety.py
Code(コーディングエージェント)の作業フォルダに関する安全管理。

  - OS / システムの重要フォルダ(例: C:\\Windows, /etc, /usr ...)
  - ファイルシステム / ドライブのルート(例: C:\\, /)
  - 複数ユーザーの共有ルート(例: C:\\Users, /home)
  - アプリ自身のデータ領域(DB・鍵・ベクトルストア)

を作業フォルダに「選べない・書き込めない」ようにする。これにより、AIが
書き換えできる範囲を安全なフォルダ(=利用者が明示的に選んだ専用フォルダ)に
限定する。
"""
from __future__ import annotations

import os
from pathlib import Path

from .config import ROOT_DIR, settings
from .logging_setup import get_logger

log = get_logger("safety")



def _norm(p: Path) -> Path:
    try:
        return p.resolve()
    except Exception:
        log.debug("_norm: 例外を無視して継続", exc_info=True)
        return p.absolute()


def _build_deny_tree() -> list[Path]:
    """このフォルダ自身・配下とも作業フォルダにできない重要ディレクトリ。"""
    raw: list[str] = []
    if os.name == "nt":
        env = os.environ
        for k in ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)",
                  "ProgramW6432", "ProgramData", "CommonProgramFiles",
                  "CommonProgramFiles(x86)", "AllUsersProfile"):
            v = env.get(k)
            if v:
                raw.append(v)
        sysdrive = env.get("SystemDrive", "C:")
        for sub in ("Windows", "Program Files", "Program Files (x86)", "ProgramData",
                    "$Recycle.Bin", "System Volume Information", "Recovery", "PerfLogs"):
            raw.append(f"{sysdrive}\\{sub}")
    else:
        raw += [
            "/bin", "/sbin", "/usr", "/lib", "/lib32", "/lib64", "/libx32",
            "/etc", "/boot", "/dev", "/proc", "/sys", "/run", "/var",
            "/opt", "/srv", "/root",
            # macOS
            "/System", "/Library", "/private", "/cores", "/Applications",
        ]
    out: list[Path] = []
    for d in raw:
        try:
            out.append(_norm(Path(d)))
        except Exception:
            log.debug("_build_deny_tree: 例外を無視して継続", exc_info=True)
            pass
    # アプリ自身のデータ領域(secret.key / DB / chroma / uploads)も保護
    try:
        out.append(_norm(settings.data_dir))
    except Exception:
        log.debug("_build_deny_tree: 例外を無視して継続", exc_info=True)
        pass
    return out


def _build_deny_exact() -> list[Path]:
    """このフォルダ「ちょうど」は不可だが、配下のサブフォルダは可(ユーザー共有ルート)。"""
    raw: list[str] = ["/home", "/Users", "/mnt", "/media", "/Volumes"]
    out: list[Path] = []
    for d in raw:
        try:
            out.append(_norm(Path(d)))
        except Exception:
            log.debug("_build_deny_exact: 例外を無視して継続", exc_info=True)
            pass
    try:
        # 例: C:\\Users, /home(各ユーザーのホームの親)
        out.append(_norm(Path.home().parent))
    except Exception:
        log.debug("_build_deny_exact: 例外を無視して継続", exc_info=True)
        pass
    return out


def _build_deny_files() -> list[Path]:
    """フォルダではなく「このファイルだけ」を保護する機密ファイル。

    自アプリの .env(CHAT_PASSWORD / OCR_API_KEY 等を含む)が対象。作業フォルダに
    アプリ自身のフォルダを選んだ場合でも、エージェントから読み書きできないようにする
    (他プロジェクトの .env は対象外=通常の開発作業は妨げない)。
    """
    out: list[Path] = []
    try:
        out.append(_norm(ROOT_DIR / ".env"))
    except Exception:
        log.debug("_build_deny_files: 例外を無視して継続", exc_info=True)
    return out


_DENY_TREE = _build_deny_tree()
_DENY_EXACT = _build_deny_exact()
_DENY_FILES = _build_deny_files()
_DENY_FILE_NAMES = {p.name for p in _DENY_FILES}


def is_protected_file(path) -> bool:
    """このファイル自体が機密保護対象(自アプリの .env 等)か。
    名前が一致するときだけパスを解決して厳密比較する(走査時の呼び出しでも軽い)。"""
    try:
        p = Path(path)
        if p.name not in _DENY_FILE_NAMES:
            return False
        return _norm(p) in _DENY_FILES
    except Exception:
        log.debug("is_protected_file: 例外を無視して継続", exc_info=True)
        return True   # 判定できないものは安全側に倒す


def _is_fs_root(p: Path) -> bool:
    return p == p.parent


def _equal_or_inside(p: Path, base: Path) -> bool:
    return p == base or base in p.parents


def is_within_protected(path) -> bool:
    """このパス(またはその親)が保護対象(システム/アプリのデータ領域/機密ファイル)か。"""
    try:
        p = _norm(Path(path))
    except Exception:
        log.debug("is_within_protected: 例外を無視して継続", exc_info=True)
        return True  # 判定できないものは安全側に倒す
    if p in _DENY_FILES:
        return True
    return any(_equal_or_inside(p, d) for d in _DENY_TREE)


def check_workspace(path) -> tuple[bool, str]:
    """
    作業フォルダとして許可できるか判定する。
    戻り値: (ok, 理由)。ok=False のとき理由を表示に使う。
    """
    if not path or not str(path).strip():
        return False, "フォルダが指定されていません"

    p = _norm(Path(path).expanduser())

    if not p.exists() or not p.is_dir():
        return False, "フォルダが存在しません"

    if _is_fs_root(p):
        return False, "ドライブ/ファイルシステムのルートは作業フォルダにできません"

    for d in _DENY_EXACT:
        if p == d:
            return False, "共有ルート(ユーザー全体など)は不可です。専用のサブフォルダを選んでください"

    for d in _DENY_TREE:
        if _equal_or_inside(p, d):
            return False, "OS・システム上の重要フォルダは作業フォルダにできません"

    return True, ""
