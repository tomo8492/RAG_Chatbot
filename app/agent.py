"""
agent.py
Web版コーディングエージェント(Claude Code 風)。
指定された「作業フォルダ」の中だけで、ローカルLLM(Ollama)が
  - ファイル一覧 / 読み取り
  - ファイルの作成・上書き(write_file)
  - シェルコマンドの実行(run_command)
を行い、依頼を達成する。

変更系ツール(write_file / run_command)は、呼び出し側(Web)で
ユーザーの承認を得てから実行する(承認レジストリ経由)。すべての
ファイル操作は作業フォルダ内に限定される。
"""
from __future__ import annotations

import difflib
import json
import os
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Iterator, Optional

import ollama

from .config import settings
from .logging_setup import get_logger

log = get_logger("agent")

MAX_STEPS = 40
CMD_TIMEOUT = 120
CONFIRM_TIMEOUT = 600  # 承認待ちの最大秒数

SYSTEM_PROMPT = """あなたは優秀なソフトウェアエンジニアのエージェントです。
指定された「作業フォルダ」の中だけで、ユーザーの依頼を達成します。

進め方:
1. まず list_files / read_file で現状を把握する。
2. ファイルの作成・変更は write_file を使う(変更後のファイル内容の全文を渡す)。
3. 実行・テスト・ビルドが必要なら run_command を使う。
4. パスはすべて作業フォルダからの相対パスで指定する。作業フォルダの外は操作しない。
5. write_file と run_command はユーザーの承認が必要。拒否された場合は無理に進めず、別の方法や確認を提案する。
6. 不明点は推測しすぎず、必要なら質問する。
7. 作業が完了したら、ツールを呼ばずに日本語で「何をしたか」を簡潔に要約して終了する。
"""

TOOLS = [
    {"type": "function", "function": {
        "name": "list_files", "description": "作業フォルダ内のファイル一覧(相対パス)を返す",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "read_file", "description": "指定ファイルの内容を読み取る",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "作業フォルダからの相対パス"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file", "description": "ファイルを作成または上書きする(内容の全文を渡す)",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "作業フォルダからの相対パス"},
            "content": {"type": "string", "description": "ファイルの内容(全文)"}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "run_command", "description": "作業フォルダでシェルコマンドを実行し、出力を返す",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "実行するコマンド"}},
            "required": ["command"]}}},
]

READONLY = {"list_files", "read_file"}
MUTATING = {"write_file", "run_command"}

IGNORE_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode", "dist", "build"}


# ============================================================
#  ツール実装(すべて作業フォルダ内に限定)
# ============================================================
def _safe_path(ws: Path, rel: str) -> Path:
    p = (ws / rel).resolve()
    if p != ws and ws not in p.parents:
        raise ValueError(f"作業フォルダ外は操作できません: {rel}")
    return p


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


def t_write_file(ws: Path, path: str, content: str) -> str:
    try:
        p = _safe_path(ws, path)
    except ValueError as e:
        return f"[エラー] {e}"
    existed = p.exists()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        verb = "上書き" if existed else "作成"
        return f"[OK] {verb}しました: {path} ({len(content)}文字)"
    except Exception as e:
        return f"[エラー] 書き込み失敗: {e}"


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


def dispatch(ws: Path, name: str, args: dict) -> str:
    if name == "list_files":
        return t_list_files(ws)
    if name == "read_file":
        return t_read_file(ws, args.get("path", ""))
    if name == "write_file":
        return t_write_file(ws, args.get("path", ""), args.get("content", ""))
    if name == "run_command":
        return t_run_command(ws, args.get("command", ""))
    return f"[エラー] 未知のツール: {name}"


# ============================================================
#  承認レジストリ(変更系ツールの実行可否をWeb側から受け取る)
# ============================================================
_pending: dict[str, dict] = {}
_pending_lock = threading.Lock()


def new_pending() -> str:
    aid = uuid.uuid4().hex
    with _pending_lock:
        _pending[aid] = {"event": threading.Event(), "approved": False}
    return aid


def resolve(action_id: str, approved: bool) -> bool:
    """Web のボタン押下から呼ばれる。承認/拒否を記録して待機側を起こす。"""
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
#  承認カード/ステップ表示用の整形
# ============================================================
def _preview_args(name: str, args: dict) -> dict:
    if name == "write_file":
        return {"path": args.get("path", ""), "length": len(args.get("content", "") or "")}
    if name == "run_command":
        return {"command": args.get("command", "")}
    if name == "read_file":
        return {"path": args.get("path", "")}
    return {}


def _confirm_detail(ws: Path, name: str, args: dict) -> dict:
    """承認カードに出す詳細(write_file は差分、run_command はコマンド)。"""
    if name == "write_file":
        path = args.get("path", "")
        content = args.get("content", "") or ""
        old = ""
        exists = False
        try:
            p = _safe_path(ws, path)
            if p.exists() and p.is_file():
                exists = True
                old = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass
        diff = "\n".join(difflib.unified_diff(
            old.splitlines(), content.splitlines(),
            fromfile=(path + " (現在)") if exists else "(新規)",
            tofile=path + " (変更後)", lineterm="",
        ))
        diff = diff[:8000] + ("\n...(差分省略)" if len(diff) > 8000 else "")
        return {"path": path, "exists": exists, "length": len(content), "diff": diff}
    if name == "run_command":
        return {"command": args.get("command", "")}
    return {}


# ============================================================
#  エージェントループ(イベントを yield)
# ============================================================
def _client():
    return ollama.Client(host=settings.ollama_host)


def run_stream(model: str, messages: list, workspace: str,
               allow_changes: bool) -> Iterator[dict]:
    """
    エージェントを1依頼ぶん実行し、イベントを順次 yield する。
    イベント type:
      assistant     : エージェントの発話(text)
      tool_call     : ツール呼び出し(name, args)
      tool_result   : ツール結果(name, status[ok|blocked|rejected], result)
      confirm       : 変更系ツールの承認要求(action_id, name, 詳細)
      done          : 完了
      max_steps     : 最大ステップ到達
      error         : エラー(error)
    messages は会話文脈(in/out で更新される)。
    """
    ws = Path(workspace).resolve()
    client = _client()

    for _ in range(MAX_STEPS):
        try:
            resp = client.chat(model=model, messages=messages, tools=TOOLS)
        except Exception as e:
            emsg = str(e)
            log.warning("agent 生成失敗: %s", emsg)
            if "tool" in emsg.lower():
                emsg = (f"{emsg}\n(このモデルはツール呼び出しに未対応かもしれません。"
                        "qwen3 等のツール対応モデルをお試しください)")
            yield {"type": "error", "error": emsg}
            return

        msg = resp.message
        messages.append(msg)

        content = getattr(msg, "content", None)
        if content:
            yield {"type": "assistant", "text": content}

        tool_calls = getattr(msg, "tool_calls", None)
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

            yield {"type": "tool_call", "name": name, "args": _preview_args(name, args)}

            if name in MUTATING:
                if not allow_changes:
                    result = ("[変更は許可されていません。画面上部の『変更を許可』を"
                              "オンにすると、承認のうえで変更・実行できます(読み取りは可能です)]")
                    yield {"type": "tool_result", "name": name, "status": "blocked", "result": result}
                else:
                    aid = new_pending()
                    detail = _confirm_detail(ws, name, args)
                    yield {"type": "confirm", "action_id": aid, "name": name, **detail}
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
                result = dispatch(ws, name, args)
                yield {"type": "tool_result", "name": name, "status": "ok", "result": result}

            messages.append({"role": "tool", "content": str(result), "tool_name": name})

    yield {"type": "max_steps"}
