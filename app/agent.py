"""
agent.py
Web版コーディングエージェント(Claude Code 風)。

指定された「作業フォルダ」の中だけで、ローカルLLM(Ollama)が
  - 調査(読み取り): list_files / read_file / glob / grep
  - 変更: write_file(全文) / edit_file(部分置換) / run_command(コマンド)
  - present_plan: 実行計画を提示して承認を得る(計画モード)
を行い、依頼を達成する。

計画モード(既定)では「調査 → 計画提示 → 承認 → 実行」の順で進む。
承認後の実行フェーズでは、ファイル編集は自動適用、run_command など重要操作は
そのつど承認を取る。計画モードを切ると、従来どおり「変更を許可」+毎回承認で動く。
すべてのファイル操作は作業フォルダ内に限定される。
"""
from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Iterator, Optional

import ollama

from . import safety
from .config import settings
from .logging_setup import get_logger

log = get_logger("agent")

MAX_STEPS = 40
CMD_TIMEOUT = 120
CONFIRM_TIMEOUT = 600     # 承認待ちの最大秒数
MAX_GREP_FILE = 2_000_000  # grep で読むファイルの上限(2MB)

SYSTEM_PROMPT = """あなたは優秀なソフトウェアエンジニアのエージェントです。
指定された「作業フォルダ」の中だけで、ユーザーの依頼を達成します。

【ツール】
- 調査(読み取り): list_files / read_file / glob(ファイル名検索) / grep(内容検索)
- 変更: write_file(新規作成・全文上書き) / edit_file(既存ファイルの一部置換) / run_command(短時間コマンド)
- 長時間処理: run_background(devサーバ等。job_idを返す) / command_output(出力確認) / stop_command(停止)
- 進捗管理: todo_write(タスクのチェックリストを更新。多段作業で活用)
- present_plan: 実行計画を提示してユーザーの承認を得る(計画モードのとき)

【進め方】
1. まず glob / grep / read_file で現状を十分に調査する。
2. 計画モードでは、調査が済んだら present_plan で「実行計画(番号付きの手順)」を提示し、承認を待つ。
   承認されるまでファイルの変更やコマンド実行はできない。
3. 承認後(または計画モードでないとき)は、計画に沿って実行する。
   既存ファイルの部分的な修正は write_file(全文)ではなく edit_file を優先する。
4. パスはすべて作業フォルダからの相対パス。作業フォルダの外は操作しない。
5. run_command などの重要操作はユーザー確認が入る。拒否されたら無理に進めず別案を出す。
6. 作業が完了したら、ツールを呼ばずに日本語で「何をしたか」を簡潔に要約して終了する。

【Excel/Word/PowerPoint/PDF など"本物のファイル"の作成】
「Excelで」「PowerPoint(pptx)で」「PDFで」等を求められたら、Markdownではなく実ファイルを作る。
手順: write_file で生成用 Python スクリプトを作り、run_command で実行して作業フォルダに出力する。
利用ライブラリ(インストール済み):
- Excel(.xlsx): openpyxl。数式は文字列で代入(例: ws["C2"]="=SUM(A2:B2)")。グラフは openpyxl.chart(BarChart/LineChart 等)。複数シート可。
- Word(.docx): python-docx(見出し・段落・表・箇条書き)。
- PowerPoint(.pptx): python-pptx(タイトル+箇条書きスライド)。
- PDF(.pdf): reportlab。
作成後は list_files / run_command で出力を確認する。Python は run_command で `python スクリプト.py` のように実行する。
"""

# ---- ツール定義(スキーマ) ----
_T_LIST = {"type": "function", "function": {
    "name": "list_files", "description": "作業フォルダ内のファイル一覧(相対パス)を返す",
    "parameters": {"type": "object", "properties": {}, "required": []}}}
_T_READ = {"type": "function", "function": {
    "name": "read_file", "description": "指定ファイルの内容を読み取る",
    "parameters": {"type": "object", "properties": {
        "path": {"type": "string", "description": "作業フォルダからの相対パス"}},
        "required": ["path"]}}}
_T_GLOB = {"type": "function", "function": {
    "name": "glob", "description": "globパターンでファイルを検索する(例: **/*.py, src/**/*.ts)",
    "parameters": {"type": "object", "properties": {
        "pattern": {"type": "string", "description": "globパターン"}},
        "required": ["pattern"]}}}
_T_GREP = {"type": "function", "function": {
    "name": "grep", "description": "ファイル内容を正規表現で横断検索する。一致した ファイル:行番号:行 を返す",
    "parameters": {"type": "object", "properties": {
        "pattern": {"type": "string", "description": "検索する正規表現"},
        "path_glob": {"type": "string", "description": "対象を絞るglob(任意。例 **/*.py)"}},
        "required": ["pattern"]}}}
_T_WRITE = {"type": "function", "function": {
    "name": "write_file", "description": "ファイルを新規作成または全文上書きする",
    "parameters": {"type": "object", "properties": {
        "path": {"type": "string", "description": "作業フォルダからの相対パス"},
        "content": {"type": "string", "description": "ファイルの内容(全文)"}},
        "required": ["path", "content"]}}}
_T_EDIT = {"type": "function", "function": {
    "name": "edit_file",
    "description": "既存ファイルの一部を置換する。old_string は一意に決まるよう十分な文脈を含めること",
    "parameters": {"type": "object", "properties": {
        "path": {"type": "string", "description": "作業フォルダからの相対パス"},
        "old_string": {"type": "string", "description": "置換前の文字列(現在のファイルに存在する内容)"},
        "new_string": {"type": "string", "description": "置換後の文字列"},
        "replace_all": {"type": "boolean", "description": "すべての一致を置換する場合 true(任意)"}},
        "required": ["path", "old_string", "new_string"]}}}
_T_CMD = {"type": "function", "function": {
    "name": "run_command", "description": "作業フォルダでシェルコマンドを実行し、出力を返す(短時間で終わるもの向け)",
    "parameters": {"type": "object", "properties": {
        "command": {"type": "string", "description": "実行するコマンド"}},
        "required": ["command"]}}}
_T_BG = {"type": "function", "function": {
    "name": "run_background",
    "description": "長時間動くコマンド(devサーバ・ウォッチ等)をバックグラウンドで起動する。job_id を返す",
    "parameters": {"type": "object", "properties": {
        "command": {"type": "string", "description": "実行するコマンド"}},
        "required": ["command"]}}}
_T_BGOUT = {"type": "function", "function": {
    "name": "command_output", "description": "バックグラウンドjobの現在の出力と状態を取得する",
    "parameters": {"type": "object", "properties": {
        "job_id": {"type": "string", "description": "run_background が返した job_id"}},
        "required": ["job_id"]}}}
_T_BGSTOP = {"type": "function", "function": {
    "name": "stop_command", "description": "バックグラウンドjobを停止する",
    "parameters": {"type": "object", "properties": {
        "job_id": {"type": "string", "description": "停止する job_id"}},
        "required": ["job_id"]}}}
_T_PLAN = {"type": "function", "function": {
    "name": "present_plan",
    "description": "調査が終わったら、これから行う実行計画を提示してユーザーの承認を得る",
    "parameters": {"type": "object", "properties": {
        "plan": {"type": "string", "description": "実行計画(Markdown。番号付きの手順で簡潔に)"}},
        "required": ["plan"]}}}
_T_TODO = {"type": "function", "function": {
    "name": "todo_write",
    "description": "タスクの進捗チェックリストを更新する。多段の作業では計画/進捗をこれで管理する。毎回 todos 全体を渡す。",
    "parameters": {"type": "object", "properties": {
        "todos": {"type": "array", "description": "タスク一覧",
                  "items": {"type": "object", "properties": {
                      "content": {"type": "string", "description": "タスク内容"},
                      "status": {"type": "string", "enum": ["pending", "in_progress", "completed"],
                                 "description": "状態(pending/in_progress/completed)"}},
                      "required": ["content", "status"]}}},
        "required": ["todos"]}}}

READ_TOOLS = [_T_LIST, _T_READ, _T_GLOB, _T_GREP]
WRITE_TOOLS = [_T_WRITE, _T_EDIT, _T_CMD, _T_BG]
META_TOOLS = [_T_BGOUT, _T_BGSTOP]   # 確認不要のメタ操作(jobの出力取得・停止)
PLAN_PHASE_TOOLS = READ_TOOLS + [_T_TODO, _T_PLAN]
EXEC_PHASE_TOOLS = READ_TOOLS + WRITE_TOOLS + META_TOOLS + [_T_TODO]

READONLY = {"list_files", "read_file", "glob", "grep"}
MUTATING = {"write_file", "edit_file", "run_command", "run_background"}
META = {"command_output", "stop_command"}        # 常に許可・確認不要
CONFIRM_IN_EXEC = {"run_command", "run_background"}   # 計画承認後でも確認する重要操作

IGNORE_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode", "dist", "build"}

# プロジェクト指示(CLAUDE.md 等)。作業フォルダ直下にあれば自動で読み込む。
PROJECT_FILES = ["CLAUDE.md", "AGENTS.md", ".claude/CLAUDE.md"]


def read_project_instructions(ws: Path, limit: int = 8000) -> Optional[str]:
    """作業フォルダの CLAUDE.md / AGENTS.md を読み、エージェントへの指示として返す。"""
    for name in PROJECT_FILES:
        try:
            p = (ws / name)
            if p.is_file():
                text = p.read_text(encoding="utf-8", errors="replace").strip()
                if text:
                    return text[:limit] + ("\n...(省略)" if len(text) > limit else "")
        except Exception:
            continue
    return None


def _norm_todos(todos) -> list:
    """todo_write の引数を正規化(content/status のみ・状態を検証)。"""
    out = []
    if isinstance(todos, list):
        for t in todos:
            if not isinstance(t, dict):
                continue
            content = str(t.get("content") or t.get("task") or "").strip()
            status = str(t.get("status") or "pending").strip()
            if status not in ("pending", "in_progress", "completed"):
                status = "pending"
            if content:
                out.append({"content": content, "status": status})
    return out


# ============================================================
#  ツール実装(すべて作業フォルダ内に限定)
# ============================================================
def _safe_path(ws: Path, rel: str) -> Path:
    p = (ws / rel).resolve()
    if p != ws and ws not in p.parents:
        raise ValueError(f"作業フォルダ外は操作できません: {rel}")
    # 作業フォルダ配下でも、OS/システムやアプリのデータ領域は触らせない
    if safety.is_within_protected(p):
        raise ValueError(f"保護されたフォルダのため操作できません: {rel}")
    return p


def _rel_ok(ws: Path, p: Path) -> bool:
    try:
        rel = p.relative_to(ws)
    except ValueError:
        return False
    return not any(part in IGNORE_DIRS for part in rel.parts)


def t_list_files(ws: Path) -> str:
    out = []
    for root, dirs, files in os.walk(ws):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
        for f in files:
            out.append(str(Path(os.path.relpath(os.path.join(root, f), ws)).as_posix()))
            if len(out) >= 500:
                return "\n".join(out) + "\n...(500件で省略)"
    return "\n".join(out) if out else "(空のフォルダ)"


def t_read_file(ws: Path, path: str) -> str:
    try:
        p = _safe_path(ws, path)
    except ValueError as e:
        return f"[エラー] {e}"
    if not p.exists() or not p.is_file():
        return f"[エラー] ファイルが存在しません: {path}"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
        return text[:20000] + ("\n...(20000文字で省略)" if len(text) > 20000 else "")
    except Exception as e:
        return f"[エラー] 読み取り失敗: {e}"


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
        return f"[エラー] glob失敗: {e}"


def t_grep(ws: Path, pattern: str, path_glob: Optional[str] = None, max_matches: int = 200) -> str:
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"[エラー] 正規表現が不正です: {e}"
    try:
        it = ws.glob(path_glob) if path_glob else ws.rglob("*")
    except Exception as e:
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
        return f"[エラー] {e}"
    existed = p.exists()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"[OK] {'上書き' if existed else '作成'}しました: {path} ({len(content)}文字)"
    except Exception as e:
        return f"[エラー] 書き込み失敗: {e}"


def t_edit_file(ws: Path, path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    try:
        p = _safe_path(ws, path)
    except ValueError as e:
        return f"[エラー] {e}"
    if not p.exists() or not p.is_file():
        return f"[エラー] ファイルが存在しません: {path}"
    if not old_string:
        return "[エラー] old_string が空です"
    if old_string == new_string:
        return "[エラー] old_string と new_string が同一です"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"[エラー] 読み取り失敗: {e}"
    cnt = text.count(old_string)
    if cnt == 0:
        return "[エラー] old_string が見つかりません(文脈を増やして再指定してください)"
    if cnt > 1 and not replace_all:
        return f"[エラー] old_string が {cnt} 箇所に一致します。文脈を増やすか replace_all=true を指定してください"
    new_text = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
    try:
        p.write_text(new_text, encoding="utf-8")
    except Exception as e:
        return f"[エラー] 書き込み失敗: {e}"
    return f"[OK] 編集しました: {path} ({cnt if replace_all else 1}箇所)"


def t_run_command(ws: Path, command: str) -> str:
    try:
        r = subprocess.run(command, shell=True, cwd=str(ws),
                           capture_output=True, text=True, timeout=CMD_TIMEOUT)
        out = (r.stdout or "") + (r.stderr or "")
        out = out[:8000] + ("\n...(出力省略)" if len(out) > 8000 else "")
        return f"[終了コード {r.returncode}]\n{out or '(出力なし)'}"
    except subprocess.TimeoutExpired:
        return f"[エラー] タイムアウト({CMD_TIMEOUT}秒)しました"
    except Exception as e:
        return f"[エラー] 実行失敗: {e}"


# ---- バックグラウンドジョブ(長時間コマンド) ----
_bg_jobs: dict[str, dict] = {}
_bg_lock = threading.Lock()
MAX_BG_JOBS = 10
BG_OUTPUT_CAP = 20000


def _bg_reader(job_id: str, proc: "subprocess.Popen") -> None:
    try:
        for line in proc.stdout:                     # 行ごとにバッファへ
            with _bg_lock:
                j = _bg_jobs.get(job_id)
                if j is None:
                    break
                j["output"] = (j["output"] + line)[-BG_OUTPUT_CAP:]
    except Exception:
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
        return f"[エラー] 起動失敗: {e}"
    job_id = uuid.uuid4().hex[:8]
    with _bg_lock:
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
            proc.kill()
            rc = proc.wait()
    except Exception as e:
        return f"[エラー] 停止失敗: {e}"
    with _bg_lock:                       # 停止直後に状態を確定(リーダースレッド待ちにしない)
        j = _bg_jobs.get(job_id)
        if j is not None:
            j["running"] = False
            if j["returncode"] is None:
                j["returncode"] = rc
    return f"[OK] job {job_id} を停止しました"


def dispatch(ws: Path, name: str, args: dict) -> str:
    if name == "list_files":
        return t_list_files(ws)
    if name == "read_file":
        return t_read_file(ws, args.get("path", ""))
    if name == "glob":
        return t_glob(ws, args.get("pattern", ""))
    if name == "grep":
        return t_grep(ws, args.get("pattern", ""), args.get("path_glob"))
    if name == "write_file":
        return t_write_file(ws, args.get("path", ""), args.get("content", ""))
    if name == "edit_file":
        return t_edit_file(ws, args.get("path", ""), args.get("old_string", ""),
                           args.get("new_string", ""), bool(args.get("replace_all")))
    if name == "run_command":
        return t_run_command(ws, args.get("command", ""))
    if name == "run_background":
        return t_run_background(ws, args.get("command", ""))
    if name == "command_output":
        return t_command_output(args.get("job_id", ""))
    if name == "stop_command":
        return t_stop_command(args.get("job_id", ""))
    return f"[エラー] 未知のツール: {name}"


# ============================================================
#  承認レジストリ(計画承認・変更系の確認を Web 側から受け取る)
# ============================================================
_pending: dict[str, dict] = {}
_pending_lock = threading.Lock()


def new_pending() -> str:
    aid = uuid.uuid4().hex
    with _pending_lock:
        _pending[aid] = {"event": threading.Event(), "approved": False}
    return aid


def resolve(action_id: str, approved: bool) -> bool:
    with _pending_lock:
        p = _pending.get(action_id)
    if not p:
        return False
    p["approved"] = bool(approved)
    p["event"].set()
    return True


def wait(action_id: str, timeout: float = CONFIRM_TIMEOUT) -> Optional[bool]:
    """承認待ち。True=承認 / False=拒否 / None=タイムアウト。"""
    with _pending_lock:
        p = _pending.get(action_id)
    if not p:
        return None
    ok = p["event"].wait(timeout)
    with _pending_lock:
        p = _pending.pop(action_id, None)
    if not ok or p is None:
        return None
    return p["approved"]


# ============================================================
#  表示用の整形
# ============================================================
def _preview_args(name: str, args: dict) -> dict:
    if name == "write_file":
        return {"path": args.get("path", ""), "length": len(args.get("content", "") or "")}
    if name == "edit_file":
        return {"path": args.get("path", "")}
    if name in ("run_command", "run_background"):
        return {"command": args.get("command", "")}
    if name in ("command_output", "stop_command"):
        return {"job_id": args.get("job_id", "")}
    if name == "read_file":
        return {"path": args.get("path", "")}
    if name in ("glob", "grep"):
        return {"pattern": args.get("pattern", "")}
    return {}


def _change_preview(ws: Path, name: str, args: dict) -> dict:
    """write_file / edit_file の変更後を予測し、差分(unified diff)を作る。"""
    path = args.get("path", "")
    old = ""
    exists = False
    try:
        p = _safe_path(ws, path)
        if p.exists() and p.is_file():
            exists = True
            old = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if name == "write_file":
        new = args.get("content", "") or ""
    else:  # edit_file
        old_s = args.get("old_string", "") or ""
        new_s = args.get("new_string", "") or ""
        if old_s and old_s in old:
            new = old.replace(old_s, new_s) if args.get("replace_all") else old.replace(old_s, new_s, 1)
        else:
            new = old  # 一致なし(実行時にエラーになる)
    diff = "\n".join(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile=(path + " (現在)") if exists else "(新規)",
        tofile=path + " (変更後)", lineterm="",
    ))
    diff = diff[:8000] + ("\n...(差分省略)" if len(diff) > 8000 else "")
    return {"path": path, "exists": exists, "diff": diff}


def _action_detail(ws: Path, name: str, args: dict) -> dict:
    if name in ("run_command", "run_background"):
        return {"command": args.get("command", "")}
    if name in ("write_file", "edit_file"):
        return _change_preview(ws, name, args)
    return {}


# ============================================================
#  コンテキスト自動圧縮(長くなったら古い履歴を要約に置換)
# ============================================================
CTX_CHAR_LIMIT = 60000   # 文脈の合計文字数がこれを超えたら圧縮


def _text_of(m) -> str:
    if isinstance(m, dict):
        return str(m.get("content") or "")
    return str(getattr(m, "content", "") or "")


def _role_of(m) -> str:
    return m.get("role") if isinstance(m, dict) else getattr(m, "role", "")


def _ctx_chars(messages: list) -> int:
    return sum(len(_text_of(m)) for m in messages)


def _head_len(messages: list) -> int:
    """先頭の設定メッセージ(system+作業フォルダ+CLAUDE.md+ack)までの数。"""
    for i, m in enumerate(messages):
        if _role_of(m) == "assistant":
            return i + 1
    return min(len(messages), 1)


def compact_ctx(messages: list, summarizer) -> bool:
    """文脈が大きければ、設定以降を要約1件に置き換える。置換したら True。
    summarizer(transcript:str)->str。構造を壊さないよう設定部分は保持する。"""
    if _ctx_chars(messages) <= CTX_CHAR_LIMIT:
        return False
    head = _head_len(messages)
    rest = messages[head:]
    if len(rest) <= 4:
        return False
    lines = []
    for m in rest:
        txt = _text_of(m)
        if txt:
            lines.append(f"{_role_of(m)}: {txt}")
    transcript = "\n".join(lines)[-40000:]
    try:
        summary = (summarizer(transcript) or "").strip()
    except Exception:
        return False
    if not summary:
        return False
    messages[head:] = [{"role": "user", "content": "【これまでの作業の要約(自動圧縮)】\n" + summary}]
    return True


def compact_ctx_with_model(model: str, messages: list) -> bool:
    """モデルを使って文脈を圧縮(必要時のみ)。"""
    def summarizer(text: str) -> str:
        try:
            r = _client().chat(model=model, messages=[
                {"role": "system", "content": "次の作業ログを、後で作業を再開できるよう日本語で簡潔に要約してください。"
                 "重要な決定・変更したファイル・実行結果・残タスクを箇条書きで。"},
                {"role": "user", "content": text}], options={"num_predict": 400})
            return getattr(r.message, "content", "") or ""
        except Exception:
            return ""
    return compact_ctx(messages, summarizer)


def _needs_confirm(name: str, plan_mode: bool) -> bool:
    """実行フェーズで、この変更系ツールに確認が必要か。"""
    if name not in MUTATING:
        return False
    if not plan_mode:
        return True                  # 計画なし: 変更系は毎回確認(従来挙動)
    return name in CONFIRM_IN_EXEC   # 計画承認後: 重要操作(コマンド)のみ確認


# ============================================================
#  エージェントループ(イベントを yield)
# ============================================================
def _client():
    return ollama.Client(host=settings.ollama_host)


def _tc_to_dict(tc) -> dict:
    """ツール呼び出し(ollama ToolCall)を、文脈へ積むための dict に変換。"""
    fn = getattr(tc, "function", None)
    name = getattr(fn, "name", "") if fn is not None else ""
    args = getattr(fn, "arguments", {}) if fn is not None else {}
    return {"function": {"name": name, "arguments": args}}


def run_stream(model: str, messages: list, workspace: str,
               allow_changes: bool, plan_mode: bool = True) -> Iterator[dict]:
    """
    エージェントを1依頼ぶん実行し、イベントを順次 yield する。
    イベント type:
      assistant_delta / tool_call / tool_result / confirm / plan / todos / done / max_steps / error
    plan_mode=True のときは「調査→present_plan→承認→実行」。
    各ステップの本文は逐次ストリーミング(assistant_delta)で流す。
    """
    ws = Path(workspace).resolve()
    client = _client()
    phase = "plan" if plan_mode else "execute"

    for _ in range(MAX_STEPS):
        tools = PLAN_PHASE_TOOLS if phase == "plan" else EXEC_PHASE_TOOLS
        # --- 1ステップ生成(逐次ストリーミング。失敗時は非ストリームにフォールバック) ---
        content_parts: list[str] = []
        tool_calls: list = []
        streamed = False
        try:
            for chunk in client.chat(model=model, messages=messages, tools=tools, stream=True):
                cm = getattr(chunk, "message", None)
                if cm is None:
                    continue
                c = getattr(cm, "content", None)
                if c:
                    content_parts.append(c)
                    streamed = True
                    yield {"type": "assistant_delta", "text": c}
                for tc in (getattr(cm, "tool_calls", None) or []):
                    tool_calls.append(tc)
        except Exception as e:
            if streamed or tool_calls:
                log.warning("agent ストリーミング中断: %s", e)
                yield {"type": "error", "error": str(e)}
                return
            # まだ何も出力していなければ非ストリームで再試行
            try:
                resp = client.chat(model=model, messages=messages, tools=tools)
            except Exception as e2:
                emsg = str(e2)
                log.warning("agent 生成失敗: %s", emsg)
                if "tool" in emsg.lower():
                    emsg = (f"{emsg}\n(このモデルはツール呼び出しに未対応かもしれません。"
                            "qwen3 等のツール対応モデルをお試しください)")
                yield {"type": "error", "error": emsg}
                return
            cm = resp.message
            c = getattr(cm, "content", None)
            if c:
                content_parts.append(c)
                yield {"type": "assistant_delta", "text": c}
            tool_calls = list(getattr(cm, "tool_calls", None) or [])

        content = "".join(content_parts)
        asst_msg = {"role": "assistant", "content": content}
        if tool_calls:
            asst_msg["tool_calls"] = [_tc_to_dict(tc) for tc in tool_calls]
        messages.append(asst_msg)

        if not tool_calls:
            yield {"type": "done"}
            return

        for tc in tool_calls:
            name = tc.function.name
            args = tc.function.arguments
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            args = args or {}

            # --- 計画の提示と承認 ---
            if name == "present_plan":
                plan_text = (args.get("plan") or content or "(計画なし)").strip()
                aid = new_pending()
                yield {"type": "plan", "action_id": aid, "plan": plan_text}
                decision = wait(aid)
                if decision is True:
                    phase = "execute"
                    result = "[計画承認] 実行フェーズに移行しました。計画に沿って実行してください。"
                    yield {"type": "tool_result", "name": name, "status": "ok", "result": result}
                else:
                    result = ("[計画却下] ユーザーが計画を承認しませんでした。"
                              if decision is False else "[計画承認待ちがタイムアウトしました]")
                    yield {"type": "tool_result", "name": name, "status": "rejected", "result": result}
                    messages.append({"role": "tool", "content": result, "tool_name": name})
                    yield {"type": "done"}     # 追加指示を待つためここで一旦終了
                    return
                messages.append({"role": "tool", "content": result, "tool_name": name})
                continue

            # --- TODO 進捗(メタ操作・常に許可) ---
            if name == "todo_write":
                todos = _norm_todos(args.get("todos"))
                yield {"type": "todos", "todos": todos}
                result = f"[OK] TODOを更新しました({len(todos)}件)"
                messages.append({"role": "tool", "content": result, "tool_name": name})
                continue

            yield {"type": "tool_call", "name": name, "args": _preview_args(name, args)}

            # --- バックグラウンドjobの出力取得・停止(確認不要のメタ操作) ---
            if name in META:
                result = dispatch(ws, name, args)
                yield {"type": "tool_result", "name": name, "status": "ok", "result": result}
                messages.append({"role": "tool", "content": str(result), "tool_name": name})
                continue

            if name in MUTATING:
                if phase != "execute":
                    result = "[計画フェーズでは変更できません。まず present_plan で計画を提示してください]"
                    yield {"type": "tool_result", "name": name, "status": "blocked", "result": result}
                elif not plan_mode and not allow_changes:
                    result = ("[変更は許可されていません。画面の『変更を許可』をオンにすると、"
                              "承認のうえで変更・実行できます(読み取りは可能です)]")
                    yield {"type": "tool_result", "name": name, "status": "blocked", "result": result}
                elif _needs_confirm(name, plan_mode):
                    aid = new_pending()
                    yield {"type": "confirm", "action_id": aid, "name": name, **_action_detail(ws, name, args)}
                    decision = wait(aid)
                    if decision is True:
                        result = dispatch(ws, name, args)
                        yield {"type": "tool_result", "name": name, "status": "ok", "result": result}
                    elif decision is None:
                        result = "[承認がタイムアウトしたため実行しませんでした]"
                        yield {"type": "tool_result", "name": name, "status": "rejected", "result": result}
                    else:
                        result = "[ユーザーが操作を拒否しました]"
                        yield {"type": "tool_result", "name": name, "status": "rejected", "result": result}
                else:
                    # 計画承認済みのファイル編集 → 自動適用(差分を併記して透明性を確保)
                    detail = _action_detail(ws, name, args)
                    result = dispatch(ws, name, args)
                    ev = {"type": "tool_result", "name": name, "status": "ok", "result": result}
                    if detail.get("diff"):
                        ev["diff"] = detail["diff"]
                    if detail.get("path"):
                        ev["path"] = detail["path"]
                    yield ev
            else:
                result = dispatch(ws, name, args)
                yield {"type": "tool_result", "name": name, "status": "ok", "result": result}

            messages.append({"role": "tool", "content": str(result), "tool_name": name})

    yield {"type": "max_steps"}
