#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
chat_cli.py
ブラウザを使わず、ターミナルだけでチャットを試すためのスクリプト。
Webサーバ(run.py)とは独立して、Ollama / RAG の動作確認ができる。

使い方:
  python chat_cli.py                      # 通常チャット(既定モデル)
  python chat_cli.py --model qwen3-32b:latest   # モデル指定
  python chat_cli.py --folder "C:\\docs"  # フォルダを取り込んでRAGチャット
  python chat_cli.py --index <ID>         # 既存インデックスでRAGチャット

会話中のコマンド:
  /help            コマンド一覧
  /model 名前      モデル変更
  /effort レベル   工数(off/low/medium/high/max)
  /topk 数         参照件数(0で資料参照オフ)
  /len 数          回答の最大長(num_predict)
  /folder パス     フォルダを取り込んで参照資料にする
  /index           インデックス一覧から選ぶ
  /reset           会話履歴をリセット
  /exit            終了
"""
from __future__ import annotations

import argparse
import sys

# 端末の文字化け対策(Windows等)
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
except Exception:
    pass

from app import db, llm, rag
from app.config import settings
from app.defaults import get_defaults
from app.logging_setup import get_logger

log = get_logger("cli")


class CliState:
    def __init__(self, d: dict):
        self.model = d["model"]
        self.effort = d["effort"]
        self.top_k = int(d["top_k"])
        self.num_predict = int(d["num_predict"])
        self.temperature = float(d["temperature"])
        self.top_p = float(d["top_p"])
        self.system_prompt = d["system_prompt"]
        self.active_indexes: list[str] = []
        self.history: list[dict] = []


def banner():
    print("=" * 60)
    print(" 社内文書アシスタント  ターミナル版(テスト用)")
    print("=" * 60)


def pick_model(requested: str | None) -> str | None:
    models = llm.list_models()
    names = [m["name"] for m in models]
    if requested and requested in names:
        return requested
    if requested and requested not in names:
        print(f"[警告] モデル '{requested}' は未インストールです。")
    if settings.chat_model in names:
        return settings.chat_model
    if names:
        print(f"[情報] インストール済みモデルから '{names[0]}' を使用します。")
        return names[0]
    print("[エラー] 利用可能なモデルがありません。例: ollama pull qwen3:8b")
    print("         インストール済み一覧:", names or "(なし)")
    return None


def build_index_from_folder(path: str) -> str | None:
    print(f"[取り込み] {path} をインデックス化します...(初回は埋め込みモデルのDLで時間がかかります)")
    name = path.rstrip("/\\").split("/")[-1].split("\\")[-1] or path
    idx = db.create_index(name, [path])
    rag.build_index(idx["id"], [path], progress=lambda m: print("   " + m))
    res = db.get_index(idx["id"])
    if res["status"] == "ready":
        print(f"[完了] {res['file_count']}ファイル / {res['chunk_count']}チャンク")
        return idx["id"]
    print(f"[失敗] {res.get('error')}")
    return None


def choose_index() -> list[str]:
    idxs = [i for i in db.list_indexes() if i["status"] == "ready"]
    if not idxs:
        print("[情報] 利用可能なインデックスがありません。/folder で作成できます。")
        return []
    print("参照資料インデックス:")
    for n, i in enumerate(idxs, 1):
        print(f"  {n}. {i['name']}  ({i['file_count']}ファイル/{i['chunk_count']}チャンク)")
    sel = input("番号を選択(空欄=使わない): ").strip()
    if not sel:
        return []
    try:
        return [idxs[int(sel) - 1]["id"]]
    except (ValueError, IndexError):
        print("[警告] 無効な選択です。")
        return []


def stream_answer(st: CliState, question: str):
    hits = []
    if st.active_indexes and st.top_k > 0:
        try:
            hits = rag.retrieve(question, st.active_indexes, None, st.top_k)
        except Exception as e:
            print(f"[検索エラー] {e}")
    st.history.append({"role": "user", "content": question})
    messages = llm.build_messages(st.system_prompt, st.history, hits)

    print()
    acc, think = "", ""
    first_think, first_content = True, True
    try:
        for ev in llm.chat_stream(
            messages, st.model,
            temperature=st.temperature, top_p=st.top_p,
            num_predict=st.num_predict, effort=st.effort,
        ):
            if ev["type"] == "thinking":
                if first_think:
                    print("💭 [思考] ", end="", flush=True)
                    first_think = False
                think += ev["text"]
                print(ev["text"], end="", flush=True)
            else:
                if first_content:
                    if not first_think:
                        print("\n")
                    print("🤖 ", end="", flush=True)
                    first_content = False
                acc += ev["text"]
                print(ev["text"], end="", flush=True)
        print()
        st.history.append({"role": "assistant", "content": acc})
        if hits:
            srcs = []
            seen = set()
            for h in hits:
                key = (h["source"], h["loc"])
                if key not in seen:
                    seen.add(key)
                    srcs.append(f"{h['source']} {h['loc']}".strip())
            print("📎 参照:", " / ".join(srcs))
    except KeyboardInterrupt:
        print("\n[停止しました]")
        if acc:
            st.history.append({"role": "assistant", "content": acc})
    except Exception as e:
        print(f"\n[エラー] {e}")
        if acc:
            st.history.append({"role": "assistant", "content": acc})
        elif st.history and st.history[-1]["role"] == "user":
            st.history.pop()  # 失敗したターンを履歴から除去


HELP = """コマンド:
  /help          この一覧
  /model 名前    モデル変更
  /effort レベル off/low/medium/high/max
  /topk 数       参照件数(0で資料参照オフ)
  /len 数        回答の最大長
  /folder パス   フォルダを取り込んで参照資料にする
  /index         インデックス一覧から選ぶ
  /reset         会話履歴リセット
  /exit          終了"""


def handle_command(st: CliState, line: str) -> bool:
    """コマンドを処理。True=処理した。"""
    parts = line.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    if cmd in ("/exit", "/quit"):
        raise SystemExit(0)
    if cmd == "/help":
        print(HELP)
    elif cmd == "/reset":
        st.history = []
        print("[履歴をリセットしました]")
    elif cmd == "/model":
        if arg:
            st.model = arg
            print(f"[モデル] {st.model}")
        else:
            print("使い方: /model モデル名")
    elif cmd == "/effort":
        if arg in ("off", "low", "medium", "high", "max"):
            st.effort = arg
            print(f"[工数] {st.effort}")
        else:
            print("使い方: /effort off|low|medium|high|max")
    elif cmd == "/topk":
        try:
            st.top_k = max(0, int(arg))
            print(f"[参照件数] {st.top_k}")
        except ValueError:
            print("使い方: /topk 数")
    elif cmd == "/len":
        try:
            st.num_predict = max(64, int(arg))
            print(f"[回答最大長] {st.num_predict}")
        except ValueError:
            print("使い方: /len 数")
    elif cmd == "/folder":
        if arg:
            iid = build_index_from_folder(arg)
            if iid:
                st.active_indexes = [iid]
        else:
            print("使い方: /folder フォルダパス")
    elif cmd == "/index":
        st.active_indexes = choose_index()
    else:
        print(f"[不明なコマンド] {cmd}  (/help で一覧)")
    return True


def main():
    parser = argparse.ArgumentParser(description="ターミナル版チャット(テスト用)")
    parser.add_argument("--model", help="使用するOllamaモデル名")
    parser.add_argument("--folder", help="取り込むフォルダ(RAG)")
    parser.add_argument("--index", help="使用する既存インデックスID")
    args = parser.parse_args()

    banner()
    db.init_db()

    if not llm.is_ollama_available():
        print(f"[エラー] Ollama に接続できません({settings.ollama_host})")
        print("        Ollama を起動してください(Windowsは通常インストールで自動起動)。")
        print("        モデル取得例: ollama pull qwen3:8b")
        return

    st = CliState(get_defaults())
    model = pick_model(args.model)
    if not model:
        return
    st.model = model

    if args.folder:
        iid = build_index_from_folder(args.folder)
        if iid:
            st.active_indexes = [iid]
    elif args.index:
        if db.get_index(args.index):
            st.active_indexes = [args.index]
        else:
            print(f"[警告] インデックス '{args.index}' が見つかりません。")

    print(f"\nモデル: {st.model} / 工数: {st.effort} / 参照件数: {st.top_k}")
    print(f"参照資料: {'あり(' + str(len(st.active_indexes)) + '件)' if st.active_indexes else 'なし(通常チャット)'}")
    print("メッセージを入力してください。/help でコマンド一覧、/exit で終了。\n")

    while True:
        try:
            line = input("あなた> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n終了します。")
            break
        if not line:
            continue
        if line.startswith("/"):
            try:
                handle_command(st, line)
            except SystemExit:
                print("終了します。")
                break
            continue
        stream_answer(st, line)
        print()


if __name__ == "__main__":
    main()
