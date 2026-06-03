#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
code_agent.py
ターミナルで動く簡易コーディングエージェント(Claude Code 風)。
指定した「作業フォルダ」の中で、ローカルLLM(Ollama)が
  - ファイル一覧の取得 / 読み取り / 作成・上書き
  - シェルコマンドの実行(既定は実行前に確認)
を行い、依頼を達成します。すべての操作は作業フォルダ内に限定されます。

使い方(このアプリのフォルダから実行):
  python code_agent.py --folder "C:\\Users\\220557\\Documents\\myproject"
  python code_agent.py --folder . --model qwen3-32b:latest
  python code_agent.py --folder ./proj --auto        # コマンドを確認なしで実行

会話中: 依頼を入力 → エージェントが作業。/exit で終了、/reset で履歴クリア。

※ ローカルLLM(qwen3 等のツール対応モデル)が必要です。精度は Claude 本体ほどではありません。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
except Exception:
    pass

import ollama

from app.config import settings

WORKSPACE: Path = Path(".").resolve()
AUTO = False
MAX_STEPS = 40

SYSTEM_PROMPT = """あなたは優秀なソフトウェアエンジニアのエージェントです。
指定された「作業フォルダ」の中だけで、ユーザーの依頼を達成します。

進め方:
1. まず list_files / read_file で現状を把握する。
2. ファイルの作成・変更は write_file を使う(ファイル内容の全文を渡す)。
3. 実行・テスト・ビルドが必要なら run_command を使う。
4. パスはすべて作業フォルダからの相対パスで指定する。作業フォルダの外は操作しない。
5. 不明点は推測しすぎず、必要なら確認する。
6. 作業が完了したら、ツールを呼ばずに日本語で「何をしたか」を簡潔に要約して終了する。
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

IGNORE_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode", "dist", "build"}


# ============================================================
#  ツール実装(すべて作業フォルダ内に限定)
# ============================================================
def _safe_path(rel: str) -> Path:
    p = (WORKSPACE / rel).resolve()
    if p != WORKSPACE and WORKSPACE not in p.parents:
        raise ValueError(f"作業フォルダ外は操作できません: {rel}")
    return p


def t_list_files() -> str:
    import os
    out = []
    for root, dirs, files in os.walk(WORKSPACE):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
        for f in files:
            out.append(str(Path(os.path.relpath(os.path.join(root, f), WORKSPACE)).as_posix()))
            if len(out) >= 500:
                return "\n".join(out) + "\n...(500件で省略)"
    return "\n".join(out) if out else "(空のフォルダ)"


def t_read_file(path: str) -> str:
    try:
        p = _safe_path(path)
    except ValueError as e:
        return f"[エラー] {e}"
    if not p.exists() or not p.is_file():
        return f"[エラー] ファイルが存在しません: {path}"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
        return text[:20000] + ("\n...(20000文字で省略)" if len(text) > 20000 else "")
    except Exception as e:
        return f"[エラー] 読み取り失敗: {e}"


def t_write_file(path: str, content: str) -> str:
    try:
        p = _safe_path(path)
    except ValueError as e:
        return f"[エラー] {e}"
    existed = p.exists()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        verb = "上書き" if existed else "作成"
        print(f"    \033[36m✎ {verb}: {path} ({len(content)}文字)\033[0m")
        return f"[OK] {verb}しました: {path} ({len(content)}文字)"
    except Exception as e:
        return f"[エラー] 書き込み失敗: {e}"


def t_run_command(command: str) -> str:
    if not AUTO:
        try:
            ans = input(f"\n  \033[33m⚠ コマンドを実行しますか?\033[0m  $ {command}\n    [y=実行 / N=拒否]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans != "y":
            return "[ユーザーがコマンド実行を拒否しました]"
    try:
        r = subprocess.run(command, shell=True, cwd=str(WORKSPACE),
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout or "") + (r.stderr or "")
        out = out[:8000] + ("\n...(出力省略)" if len(out) > 8000 else "")
        return f"[終了コード {r.returncode}]\n{out or '(出力なし)'}"
    except subprocess.TimeoutExpired:
        return "[エラー] タイムアウト(120秒)しました"
    except Exception as e:
        return f"[エラー] 実行失敗: {e}"


def dispatch(name: str, args: dict) -> str:
    if name == "list_files":
        return t_list_files()
    if name == "read_file":
        return t_read_file(args.get("path", ""))
    if name == "write_file":
        return t_write_file(args.get("path", ""), args.get("content", ""))
    if name == "run_command":
        return t_run_command(args.get("command", ""))
    return f"[エラー] 未知のツール: {name}"


# ============================================================
#  エージェントループ
# ============================================================
def run_task(client, model: str, messages: list) -> None:
    for step in range(MAX_STEPS):
        try:
            resp = client.chat(model=model, messages=messages, tools=TOOLS)
        except Exception as e:
            print(f"\n[エラー] 生成に失敗しました: {e}")
            if "tool" in str(e).lower():
                print("  (このモデルはツール呼び出しに未対応かもしれません。qwen3 等をお試しください)")
            return

        msg = resp.message
        messages.append(msg)

        content = getattr(msg, "content", None)
        if content:
            print(f"\n\033[1m🤖\033[0m {content}")

        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            return  # 完了

        for tc in tool_calls:
            name = tc.function.name
            args = tc.function.arguments
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            preview = {k: (v[:40] + "…" if isinstance(v, str) and len(v) > 40 else v) for k, v in args.items()}
            print(f"  \033[90m▸ {name} {preview}\033[0m")
            result = dispatch(name, args or {})
            messages.append({"role": "tool", "content": str(result), "tool_name": name})

    print("\n[最大ステップ数に達しました。続けるには再度指示してください]")


def main():
    global WORKSPACE, AUTO
    parser = argparse.ArgumentParser(description="ターミナル版コーディングエージェント")
    parser.add_argument("--folder", required=True, help="作業フォルダ(この中だけで作業します)")
    parser.add_argument("--model", help="使用するOllamaモデル(既定: 設定のCHAT_MODEL)")
    parser.add_argument("--auto", action="store_true", help="コマンドを確認なしで実行する")
    args = parser.parse_args()

    WORKSPACE = Path(args.folder).expanduser().resolve()
    AUTO = args.auto
    if not WORKSPACE.is_dir():
        print(f"[エラー] フォルダが存在しません: {WORKSPACE}")
        return

    model = args.model or settings.chat_model
    client = ollama.Client(host=settings.ollama_host)
    try:
        client.list()
    except Exception:
        print(f"[エラー] Ollama に接続できません({settings.ollama_host})。`ollama serve` を確認してください。")
        return

    print("=" * 60)
    print(" コーディングエージェント(ターミナル版)")
    print("=" * 60)
    print(f" 作業フォルダ : {WORKSPACE}")
    print(f" モデル       : {model}")
    print(f" コマンド実行 : {'自動(確認なし)' if AUTO else '実行前に確認'}")
    print(" 依頼を入力してください。/exit で終了、/reset で履歴クリア。")
    print("=" * 60)

    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"作業フォルダの絶対パスは {WORKSPACE} です。"}]
    messages.append({"role": "assistant", "content": "了解しました。依頼をどうぞ。"})

    while True:
        try:
            task = input("\n\033[1m依頼>\033[0m ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n終了します。")
            break
        if not task:
            continue
        if task in ("/exit", "/quit"):
            break
        if task == "/reset":
            messages = messages[:3]
            print("[履歴をリセットしました]")
            continue
        messages.append({"role": "user", "content": task})
        try:
            run_task(client, model, messages)
        except KeyboardInterrupt:
            print("\n[中断しました]")


if __name__ == "__main__":
    main()
