"""agent のツール層。

エージェントが作業フォルダ内で使うファイル操作・検索・コマンド実行ツール(t_*)と
ディスパッチャ。承認/プレビュー/ループには依存しない(依存は一方向: _impl -> tools)。
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Optional

from .. import safety
from .constants import (
    CMD_TIMEOUT,
    IGNORE_DIRS,
    MAX_GREP_FILE,
    READ_CHAR_CAP,
    READ_DEFAULT_LINES,
    _DOC_EXTS,
    _IMG_EXTS,
)
from ..logging_setup import get_logger

log = get_logger("agent.tools")


__all__ = [
    "dispatch", "_safe_path", "_rel_ok", "check_dangerous",
    "t_list_files", "t_read_file", "t_glob", "t_grep",
    "t_write_file", "t_edit_file", "t_run_command",
    "t_run_background", "t_command_output", "t_stop_command", "t_remember",
]


def _safe_path(ws: Path, rel: str) -> Path:
    p = (ws / rel).resolve()
    if p != ws and ws not in p.parents:
        raise ValueError(f"作業フォルダ外は操作できません: {rel}")
    # 作業フォルダ配下でも、OS/システムやアプリのデータ領域・機密ファイルは触らせない
    if safety.is_within_protected(p):
        raise ValueError(f"保護された領域のため操作できません: {rel}")
    return p


def _rel_ok(ws: Path, p: Path) -> bool:
    try:
        rel = p.relative_to(ws)
    except ValueError:
        log.debug("_rel_ok: 例外を無視して継続", exc_info=True)
        return False
    if any(part in IGNORE_DIRS for part in rel.parts):
        return False
    return not safety.is_protected_file(p)   # 自アプリの .env 等は一覧・検索にも出さない


def t_list_files(ws: Path) -> str:
    out = []
    for root, dirs, files in os.walk(ws):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
        for f in files:
            if safety.is_protected_file(os.path.join(root, f)):
                continue   # 自アプリの .env 等は一覧に出さない
            out.append(str(Path(os.path.relpath(os.path.join(root, f), ws)).as_posix()))
            if len(out) >= 500:
                return "\n".join(out) + "\n...(500件で省略)"
    return "\n".join(out) if out else "(空のフォルダ)"


def t_read_file(ws: Path, path: str, offset: int = 0, limit: Optional[int] = None) -> str:
    try:
        p = _safe_path(ws, path)
    except ValueError as e:
        log.debug("t_read_file: 例外を無視して継続", exc_info=True)
        return f"[エラー] {e}"
    if not p.exists() or not p.is_file():
        return f"[エラー] ファイルが存在しません: {path}"
    ext = p.suffix.lower()
    if ext in _IMG_EXTS:
        return ("[画像ファイルです] read_file はテキスト専用です。内容を見てほしいときは、"
                "依頼に画像を添付してください(Vision対応モデルで読み取ります)。")
    if ext in _DOC_EXTS:
        try:                       # PDF/Office は loaders で本文抽出(Claude の Read 相当)
            from .. import loaders
            text = "\n\n".join(d.get("text", "") for d in loaders.load_file(p))
        except Exception as e:
            log.debug("t_read_file: 例外を無視して継続", exc_info=True)
            return f"[エラー] {ext} の読み取りに失敗: {e}"
        if not text.strip():
            return f"[{ext} から本文を抽出できませんでした(スキャンPDF等。OCRを有効化すると読めます)]"
    else:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            log.debug("t_read_file: 例外を無視して継続", exc_info=True)
            return f"[エラー] 読み取り失敗: {e}"
    lines = text.splitlines()
    total = len(lines)
    try:
        start = max(int(offset or 0), 0)
    except (TypeError, ValueError):
        log.debug("t_read_file: 例外を無視して継続", exc_info=True)
        start = 0
    if start > 0:        # offset は1始まりの行番号(0/1=先頭)
        start -= 1
    try:
        n = int(limit) if limit else READ_DEFAULT_LINES
    except (TypeError, ValueError):
        log.debug("t_read_file: 例外を無視して継続", exc_info=True)
        n = READ_DEFAULT_LINES
    n = max(n, 1)
    if total and start >= total:
        return f"[エラー] offset={start + 1} は範囲外です(全{total}行)"
    end = min(start + n, total)
    body = "\n".join(lines[start:end])
    capped = len(body) > READ_CHAR_CAP
    if capped:
        body = body[:READ_CHAR_CAP]
    notes = []
    if start > 0 or end < total:
        notes.append(f"全{total}行中 {start + 1}–{end}行を表示")
    if end < total:
        notes.append(f"続きは offset={end + 1} で読めます")
    if capped:
        notes.append(f"{READ_CHAR_CAP}文字で省略(limitを小さく)")
    if notes:
        body += ("\n" if body else "") + f"...({' / '.join(notes)})"
    return body if body else "(空のファイル)"


def t_glob(ws: Path, pattern: str) -> str:
    pattern = (pattern or "**/*").strip()
    try:
        out = []
        for p in ws.glob(pattern):
            if p.is_file() and _rel_ok(ws, p):
                out.append(p.relative_to(ws).as_posix())
                if len(out) >= 500:
                    return "\n".join(sorted(out)) + "\n...(500件で省略)"
        return "\n".join(sorted(out)) if out else "(一致なし)"
    except Exception as e:
        log.debug("t_glob: 例外を無視して継続", exc_info=True)
        return f"[エラー] glob失敗: {e}"


def t_grep(ws: Path, pattern: str, path_glob: Optional[str] = None, max_matches: int = 200) -> str:
    try:
        rx = re.compile(pattern)
    except re.error as e:
        log.debug("t_grep: 例外を無視して継続", exc_info=True)
        return f"[エラー] 正規表現が不正です: {e}"
    try:
        it = ws.glob(path_glob) if path_glob else ws.rglob("*")
    except Exception as e:
        log.debug("t_grep: 例外を無視して継続", exc_info=True)
        return f"[エラー] {e}"
    out: list[str] = []
    n = 0
    for p in it:
        if not p.is_file() or not _rel_ok(ws, p):
            continue
        try:
            if p.stat().st_size > MAX_GREP_FILE:
                continue
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            log.debug("t_grep: 例外を無視して継続", exc_info=True)
            continue
        rel = p.relative_to(ws).as_posix()
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                out.append(f"{rel}:{i}: {line.strip()[:200]}")
                n += 1
                if n >= max_matches:
                    return "\n".join(out) + "\n...(打ち切り)"
    return "\n".join(out) if out else "(一致なし)"


def t_write_file(ws: Path, path: str, content: str) -> str:
    try:
        p = _safe_path(ws, path)
    except ValueError as e:
        log.debug("t_write_file: 例外を無視して継続", exc_info=True)
        return f"[エラー] {e}"
    existed = p.exists()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"[OK] {'上書き' if existed else '作成'}しました: {path} ({len(content)}文字)"
    except Exception as e:
        log.debug("t_write_file: 例外を無視して継続", exc_info=True)
        return f"[エラー] 書き込み失敗: {e}"


def _apply_edits(text: str, edits: list) -> tuple[str, int, str]:
    """複数の置換(edits)を順に適用する。戻り: (新テキスト, 適用箇所数, エラー文 or '')。

    edits = [{old_string, new_string, replace_all?}, ...]。1件でも不一致/曖昧なら全体を中止
    (部分適用を避け、原子的に成功か失敗かにする)。"""
    total = 0
    cur = text
    for i, e in enumerate(edits, 1):
        if not isinstance(e, dict):
            return text, 0, f"[エラー] edits[{i}] が不正です"
        old = e.get("old_string") or ""
        new = e.get("new_string") or ""
        rep = bool(e.get("replace_all"))
        if not old:
            return text, 0, f"[エラー] edits[{i}] の old_string が空です"
        if old == new:
            return text, 0, f"[エラー] edits[{i}] の old_string と new_string が同一です"
        c = cur.count(old)
        if c == 0:
            return text, 0, (f"[エラー] edits[{i}] の old_string が見つかりません。read_file で"
                             "現在の内容を確認し、実在する文字列を十分な文脈つきで指定してください")
        if c > 1 and not rep:
            return text, 0, (f"[エラー] edits[{i}] の old_string が {c} 箇所に一致します。"
                             "文脈を増やすか replace_all=true を指定してください")
        cur = cur.replace(old, new) if rep else cur.replace(old, new, 1)
        total += c if rep else 1
    return cur, total, ""


def t_edit_file(ws: Path, path: str, old_string: str = "", new_string: str = "",
                replace_all: bool = False, edits: list | None = None) -> str:
    """ファイルの部分置換。単一(old_string/new_string)または複数(edits 配列)に対応。

    edits を渡すと同一ファイルの複数箇所を1回の承認・1回の書き込みで原子的に変更できる
    (1件でも不一致なら全体を中止)。"""
    try:
        p = _safe_path(ws, path)
    except ValueError as e:
        log.debug("t_edit_file: 例外を無視して継続", exc_info=True)
        return f"[エラー] {e}"
    if not p.exists() or not p.is_file():
        return f"[エラー] ファイルが存在しません: {path}"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        log.debug("t_edit_file: 例外を無視して継続", exc_info=True)
        return f"[エラー] 読み取り失敗: {e}"
    edit_list = edits if isinstance(edits, list) and edits else \
        [{"old_string": old_string, "new_string": new_string, "replace_all": replace_all}]
    new_text, cnt, err = _apply_edits(text, edit_list)
    if err:
        if not isinstance(edits, list) and not old_string:   # 単一指定で old が空の旧来エラー文
            return ("[エラー] old_string が空です。既存ファイルの全文書き換えや新規作成は "
                    "edit_file ではなく write_file を使ってください")
        return err
    try:
        p.write_text(new_text, encoding="utf-8")
    except Exception as e:
        log.debug("t_edit_file: 例外を無視して継続", exc_info=True)
        return f"[エラー] 書き込み失敗: {e}"
    n_edits = len(edit_list)
    suffix = f" / {n_edits}編集" if n_edits > 1 else ""
    return f"[OK] 編集しました: {path} ({cnt}箇所{suffix})"


# 破壊的・危険なコマンドのパターン(承認カードで強調警告する。実行はあくまでユーザー承認次第)。
# 目的は「誤承認による事故」を減らすこと。完全なサンドボックスではない点に注意。
_DANGER_PATTERNS = [
    (r"\brm\s+(-[a-z]*\s+)*-[a-z]*[rf]", "ファイルの再帰的/強制削除(rm -rf)"),
    (r"\brmdir\s+/s", "フォルダの再帰削除(rmdir /s)"),
    (r"\bdel\s+/[a-z]*[sq]", "ファイルの強制/再帰削除(del /s /q)"),
    (r"\b(format|mkfs(\.\w+)?)\b", "ディスク/パーティションのフォーマット"),
    (r"\bdd\b.*\bof=/dev/", "dd によるデバイス直接書き込み"),
    (r">\s*/dev/(sd|nvme|disk)", "ブロックデバイスへのリダイレクト"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "システムの停止/再起動"),
    (r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*f|push\s+.*--force|push\s+.*-f)\b", "Gitの破壊的操作(履歴/作業ツリーの巻き戻し)"),
    (r":\(\)\s*\{.*\};\s*:", "フォークボム"),
    (r"\bchmod\s+(-[a-z]*\s+)*0*777\b", "全権限付与(chmod 777)"),
    (r"\b(curl|wget|iwr|invoke-webrequest)\b.*\|\s*(sh|bash|python|powershell|iex)", "ダウンロードしたスクリプトの直接実行"),
    (r"\b(sudo|su)\b", "権限昇格(sudo/su)"),
    (r"\bmv\s+.*\s+/dev/null\b", "/dev/null への移動(実質削除)"),
]


def check_dangerous(command: str) -> str:
    """危険なコマンドなら理由(表示用)を、無害そうなら ''(空)を返す。"""
    s = " " + (command or "").lower() + " "
    for pat, why in _DANGER_PATTERNS:
        if re.search(pat, s):
            return why
    return ""


def t_run_command(ws: Path, command: str, timeout: int = CMD_TIMEOUT,
                  out_cap: int = 8000) -> str:
    try:
        r = subprocess.run(command, shell=True, cwd=str(ws),
                           capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or "") + (r.stderr or "")
        out = out[:out_cap] + ("\n...(出力省略)" if len(out) > out_cap else "")
        return f"[終了コード {r.returncode}]\n{out or '(出力なし)'}"
    except subprocess.TimeoutExpired:
        log.debug("t_run_command: 例外を無視して継続", exc_info=True)
        return f"[エラー] タイムアウト({timeout}秒)しました"
    except Exception as e:
        log.debug("t_run_command: 例外を無視して継続", exc_info=True)
        return f"[エラー] 実行失敗: {e}"


# ---- バックグラウンドジョブ(長時間コマンド) ----
_bg_jobs: dict[str, dict] = {}
_bg_lock = threading.Lock()
MAX_BG_JOBS = 10
BG_OUTPUT_CAP = 20000
KEEP_FINISHED_JOBS = 10   # 終了済みjobの保持数(出力参照用)。超えた古い分は破棄


def _prune_finished_jobs() -> None:
    """終了済みjobを古い順に間引く(_bg_lock 保持中に呼ぶこと)。"""
    done = [k for k, j in _bg_jobs.items() if not j["running"]]
    for k in done[:max(len(done) - KEEP_FINISHED_JOBS, 0)]:
        _bg_jobs.pop(k, None)


def _bg_reader(job_id: str, proc: "subprocess.Popen") -> None:
    out = proc.stdout
    if out is None:                                  # stdout=PIPE で起動するため通常は到達しない
        return
    try:
        for line in out:                             # 行ごとにバッファへ
            with _bg_lock:
                j = _bg_jobs.get(job_id)
                if j is None:
                    break
                j["output"] = (j["output"] + line)[-BG_OUTPUT_CAP:]
    except Exception:
        log.debug("_bg_reader: 例外を無視して継続", exc_info=True)
        pass
    finally:
        rc = proc.wait()
        with _bg_lock:
            j = _bg_jobs.get(job_id)
            if j is not None:
                j["returncode"] = rc
                j["running"] = False


def t_run_background(ws: Path, command: str) -> str:
    with _bg_lock:
        if sum(1 for j in _bg_jobs.values() if j["running"]) >= MAX_BG_JOBS:
            return "[エラー] 実行中のバックグラウンドjobが多すぎます。stop_command で停止してください"
    try:
        proc = subprocess.Popen(command, shell=True, cwd=str(ws),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
    except Exception as e:
        log.debug("t_run_background: 例外を無視して継続", exc_info=True)
        return f"[エラー] 起動失敗: {e}"
    job_id = uuid.uuid4().hex[:8]
    with _bg_lock:
        _prune_finished_jobs()
        _bg_jobs[job_id] = {"command": command, "output": "", "returncode": None,
                            "running": True, "proc": proc}
    threading.Thread(target=_bg_reader, args=(job_id, proc), daemon=True).start()
    return (f"[OK] バックグラウンドで起動しました (job_id={job_id})。"
            f"command_output で出力確認、stop_command で停止できます。")


def t_command_output(job_id: str, tail: int = 4000) -> str:
    with _bg_lock:
        j = _bg_jobs.get(job_id)
        if j is None:
            return f"[エラー] job が見つかりません: {job_id}"
        out = j["output"][-tail:]
        status = "実行中" if j["running"] else f"終了(コード {j['returncode']})"
    return f"[job {job_id} {status}]\n{out or '(出力なし)'}"


def t_stop_command(job_id: str) -> str:
    with _bg_lock:
        j = _bg_jobs.get(job_id)
        if j is None:
            return f"[エラー] job が見つかりません: {job_id}"
        proc = j["proc"]
        running = j["running"]
    if not running:
        return f"[job {job_id}] は既に終了しています"
    rc = None
    try:
        proc.terminate()
        try:
            rc = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.debug("t_stop_command: 例外を無視して継続", exc_info=True)
            proc.kill()
            rc = proc.wait()
    except Exception as e:
        log.debug("t_stop_command: 例外を無視して継続", exc_info=True)
        return f"[エラー] 停止失敗: {e}"
    with _bg_lock:                       # 停止直後に状態を確定(リーダースレッド待ちにしない)
        j = _bg_jobs.get(job_id)
        if j is not None:
            j["running"] = False
            if j["returncode"] is None:
                j["returncode"] = rc
    return f"[OK] job {job_id} を停止しました"


def dispatch(ws: Path, name: str, args: dict, cmd_timeout: int = CMD_TIMEOUT,
             out_cap: int = 8000) -> str:
    if name == "list_files":
        return t_list_files(ws)
    if name == "read_file":
        return t_read_file(ws, args.get("path", ""), args.get("offset", 0), args.get("limit"))
    if name == "glob":
        return t_glob(ws, args.get("pattern", ""))
    if name == "grep":
        return t_grep(ws, args.get("pattern", ""), args.get("path_glob"))
    if name == "write_file":
        return t_write_file(ws, args.get("path", ""), args.get("content", ""))
    if name == "edit_file":
        return t_edit_file(ws, args.get("path", ""), args.get("old_string", ""),
                           args.get("new_string", ""), bool(args.get("replace_all")),
                           args.get("edits"))
    if name == "run_command":
        return t_run_command(ws, args.get("command", ""), cmd_timeout, out_cap)
    if name == "run_background":
        return t_run_background(ws, args.get("command", ""))
    if name == "command_output":
        return t_command_output(args.get("job_id", ""))
    if name == "stop_command":
        return t_stop_command(args.get("job_id", ""))
    if name == "remember":
        return t_remember(ws, args.get("note", ""))
    return f"[エラー] 未知のツール: {name}"


def t_remember(ws: Path, note: str) -> str:
    """学んだ規約・前提を作業フォルダの CLAUDE.md に1行追記する(無ければ作成・重複は無視)。"""
    note = " ".join((note or "").split()).strip()
    if not note:
        return "[エラー] メモが空です"
    header = "## メモ(エージェントの学習)"
    try:
        p = (ws / "CLAUDE.md").resolve()
        if ws.resolve() not in p.parents and p != (ws.resolve() / "CLAUDE.md"):
            return "[エラー] 作業フォルダ外には書き込めません"
        text = p.read_text(encoding="utf-8") if p.is_file() else ""
        if ("- " + note) in text:
            return f"既に記録済み: {note}"
        if header not in text:
            text = (text.rstrip() + "\n\n" if text.strip() else "") + header + "\n"
        text = text.rstrip() + "\n- " + note + "\n"
        p.write_text(text, encoding="utf-8")
        return f"CLAUDE.md に記録しました: {note}"
    except Exception as e:
        log.debug("t_remember: 例外を無視して継続", exc_info=True)
        return f"[エラー] 記録に失敗: {e}"


# ============================================================
#  承認レジストリ(計画承認・変更系の確認を Web 側から受け取る)
