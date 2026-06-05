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
READ_DEFAULT_LINES = 800   # read_file の既定の読み取り行数
READ_CHAR_CAP = 20000      # read_file 1回の最大文字数(安全上限)

SYSTEM_PROMPT = """あなたは優秀なソフトウェアエンジニアのエージェントです。
指定された「作業フォルダ」の中だけで、ユーザーの依頼を達成します。

【ツール】
- 調査(読み取り): list_files / read_file / glob(ファイル名検索) / grep(内容検索) / summarize_path(多数ファイルの一括要約)
- 変更: write_file(新規作成・全文上書き) / edit_file(既存ファイルの一部置換) / run_command(短時間コマンド)
- 長時間処理: run_background(devサーバ等。job_idを返す) / command_output(出力確認) / stop_command(停止)
- 進捗管理: todo_write(タスクのチェックリストを更新。多段作業で活用)
- present_plan: 実行計画を提示してユーザーの承認を得る(計画モードのとき)

【進め方】
1. まず glob / grep / read_file で現状を十分に調査する。
2. 計画モードでは、調査が済んだら present_plan で「実行計画(番号付きの手順)」を提示し、承認を待つ。
   計画を承認すると、以後のファイル編集はまとめて自動適用される。
   (計画を出さずに変更しようとした場合は、1操作ずつ差分つきの確認が入る。なるべく先に計画を出すこと。)
3. 承認後(または計画モードでないとき)は、計画に沿って実行する。
   既存ファイルの部分的な修正は write_file(全文)ではなく edit_file を優先する。
4. パスはすべて作業フォルダからの相対パス。作業フォルダの外は操作しない。
5. run_command などの重要操作はユーザー確認が入る。拒否されたら無理に進めず別案を出す。
6. ask_user(質問)は乱用しない。まず「答えが欲しい質問」か「やってほしい作業」かを見極める:
   - 使い方・調べもの・原因調査などの質問(例:「どこに置けばいい?」)は聞き返さず、
     自分で調査して結論を答える。答えを選択肢にしてユーザーに選ばせない。
   - ただし作業フォルダ外の一般知識(製品仕様・バージョン・最新情報など)は確証がない場合がある。
     本ツールはWeb参照しないため、確認できない事柄は「未確認」と明示し、断定しすぎないこと。
   - 作業の進め方・対象が本当に分岐して結果が変わるときだけ ask_user で確認する。
   良い質問の作り方(あいまいな質問を避ける):
   - questions に関連する質問を1〜3個入れる。各質問は「何を決めたいか」を具体的に書き、
     header(短いラベル)を付ける(例:「DBは何を使う?」ではなく header「認証方式」+
     「ユーザー認証の保存先をどれにしますか?」のように対象と論点を明確に)。
   - 選択肢は2〜4個。各選択肢に label(短い見出し)と
     description(選ぶと何が起きるか・トレードオフを1文)を必ず付ける。
   - 複数選んでよい質問(実装する項目の取捨選択など)は multiSelect=true にする。
   - 「はい/いいえ」「おまかせ」「その他」など中身のない選択肢にしない(自由記述欄は自動で付く)。
   - 既定として妥当な案があれば、各質問で1つだけ recommended=true にする。
   - なぜ聞くのか・現状の前提があれば context に1〜2文で添える。
7. 作業が完了したら、ツールを呼ばず日本語で結果をまとめて終了する(書き方は【回答の書き方】に従う)。

【変更の適用(最重要)】
- 「変更 / 修正 / 実装 / 反映 / 適用 / 追加して」と頼まれたら、コードをチャットに貼るだけで終わらせない。
  貼っただけではファイルは1文字も変わらない。必ず edit_file / write_file で実ファイルに適用すること。
- まず read_file / grep で対象を確認し、既存の一部修正は edit_file(old_string は一意になるよう十分な文脈を含める)、
  新規作成・全面置換は write_file を使う。対象が複数なら1ファイルずつ順に適用する。
- 適用後は「どのファイルをどう変えたか」だけを簡潔に要約する。コード全文の再掲は不要(差分は自動で表示される)。

【回答の書き方(重要)】
- 必ず日本語で書く。英語に逸れない。
- 結論ファースト: まず「答え・結果」を述べる。「まず〜を調べます」「次に〜します」のような
  作業の実況や前置きは書かない。ツールログにすでに出ている内容(読んだファイル名・コマンド等)を
  本文で繰り返さない。要点だけを簡潔に。
- 次の定型句は書かない(前置き・後置き・お世辞): 「了解しました/承知しました」
  「まず〜します/次に〜します」「以上です/完了しました/対応しました」
  「お役に立てれば幸いです」「〜してみましょう」「ご確認ください」。いきなり本題に入る。
- 調べもの・使い方・原因調査の質問には、ツール操作の説明をせず結論から答え、必要なら根拠
  (該当ファイルや箇所)を短く添える。
- ファイルを変更したときは次の形でまとめる:
  1) 1行で「何を達成したか」。
  2) 変更点を箇条書き(ファイルごとに `相対パス:行番号` — 何をどう変えたか を1行)。
  3) 必要なら「確認方法」(実行コマンドやテスト手順)を1〜2行。
  ※コード全文は再掲しない(差分は自動表示される)。
- ファイル名・関数名・コマンド・識別子は `バッククォート` で等幅にする。箇条書き・短い見出しで読みやすく。
  特定の箇所を指すときは `相対パス:行番号`(例 `Common.bas:42`)の形で、必ずバッククォートで囲む
  (利用者がクリックしてその箇所を開けるようにするため)。
- 長さは内容に見合わせる。単純な質問は1〜3行、通常の変更でも本文は数行に収める。
  冗長にしない。詳しい説明は求められたときだけ。
- 確証のない前提・未確認の事項があれば最後に短く補足し、断定しすぎない。

例(変更タスク):
  ✘ 悪い例(冗長・実況・定型句):
    「承知しました。まず `Common.bas` を読み込み、構造を把握しました。次に共通の
     エラー処理がなかったため `HandleError` を追加し、続いて `Kintai.bas` を確認して
     集計ループを修正しました。以上で完了です。お役に立てれば幸いです。」
  ✔ 良い例(結論ファースト・定型・簡潔):
    勤怠集計のエラー処理を共通化し、残業計算ループのオフバイワンを修正しました。

    変更点
    - `Common.bas:18` — 共通の `HandleError` を追加(ログ出力＋通知)
    - `Kintai.bas:90` — `CalcOvertime` のループ範囲を `1 To n` → `1 To n - 1` に修正

    確認方法
    - Excelで『勤怠集計』を実行し、末日の残業が1日ぶんずれないことを確認

【Excel/Word/PowerPoint/PDF など"本物のファイル"の作成】
「Excelで」「PowerPoint(pptx)で」「PDFで」等を求められたら、Markdownではなく実ファイルを作る。
手順: write_file で生成用 Python スクリプトを作り、run_command で実行して作業フォルダに出力する。
利用ライブラリ(インストール済み):
- Excel(.xlsx): openpyxl。数式は文字列で代入(例: ws["C2"]="=SUM(A2:B2)")。グラフは openpyxl.chart(BarChart/LineChart 等)。複数シート可。
- Word(.docx): python-docx(見出し・段落・表・箇条書き)。
- PowerPoint(.pptx): python-pptx(タイトル+箇条書きスライド)。
- PDF(.pdf): reportlab。
作成後は list_files / run_command で出力を確認する。Python は run_command で `python スクリプト.py` のように実行する。

【資料・HTMLのデザイン(見た目も重視する)】
HTMLや資料を作るときは内容だけでなくデザインにも配慮する:
- CSSは<style>に埋め込み、自己完結させる(外部CDN/Webフォントに依存しない。単体で開ける)。
- 明確な階層(見出しの大小・太さ・余白)、十分なホワイトスペース、行間は1.7〜1.9、本文幅は最大~820pxに収める。
- 無彩色ベース+アクセント1色。コントラストを確保し、色は使いすぎない。
- 表はヘッダを強調+偶数行に薄い背景(ゼブラ)、引用/注記はカラーバー+淡い背景。コードは等幅+枠+角丸。
- レスポンシブ(@media)と印刷(@media print)に対応する。セマンティックなHTMLにする。
"""

# ユーザーが「変更してほしい」意図を持つかの判定(コードを貼っただけで終わるのを防ぐ安全網に使う)
_CHANGE_INTENT = re.compile(
    r"(変更|修正|直し|直す|実装|反映|適用|追加|作成|生成|実施|置換|置き換|書き換|"
    r"リファクタ|対応して|してください|して下さい|fix|change|implement|apply|refactor|add|create|update|edit|write)",
    re.I)

# コードをチャットに貼っただけで適用していないときに送る催促(1依頼につき一度だけ)
_APPLY_NUDGE = (
    "[まだ適用されていません] 直前の回答はコードをチャットに貼っただけで、ファイルは変更されていません。"
    "提示した内容を実際のファイルに反映してください: 既存ファイルの一部修正は edit_file、"
    "新規作成・全面置換は write_file を使います。中身を未確認のファイルは先に read_file で読み、"
    "edit_file の old_string が現在の内容と一致するようにしてください。"
    "すべて適用し終えたら、変更したファイルと内容を簡潔に要約してください(コード全文の再掲は不要)。"
)


# ---- ツール定義(スキーマ) ----
_T_LIST = {"type": "function", "function": {
    "name": "list_files", "description": "作業フォルダ内のファイル一覧(相対パス)を返す",
    "parameters": {"type": "object", "properties": {}, "required": []}}}
_T_READ = {"type": "function", "function": {
    "name": "read_file",
    "description": "指定ファイルの内容を読み取る。大きいファイルは offset(開始行)/limit(行数)で続きも読める",
    "parameters": {"type": "object", "properties": {
        "path": {"type": "string", "description": "作業フォルダからの相対パス"},
        "offset": {"type": "integer", "description": "開始行(1始まり。省略時は先頭)"},
        "limit": {"type": "integer", "description": "読み取る行数(省略時は既定)"}},
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
_T_ASK = {"type": "function", "function": {
    "name": "ask_user",
    "description": (
        "作業の進め方・対象が本当に分岐して結果が変わるときだけ、推測せずユーザーへ確認する。"
        "使い方・調べものの質問は聞き返さず自分で調べて答えること(答えを選択肢にしない)。"
        "questions に関連する質問を1〜3個入れる。各質問は1つの論点に絞って具体的に書き、"
        "header(短いラベル)と options(2〜4個の選択肢)を付ける。"
        "複数選んでよい質問(実装する項目の取捨選択など)は multiSelect=true にする。"
        "各選択肢には label と description(選ぶと何が起きるか/トレードオフを1文)を付け、"
        "妥当な既定があれば各質問で1つだけ recommended=true にする。"),
    "parameters": {"type": "object", "properties": {
        "context": {"type": "string",
                    "description": "なぜ確認が必要か・前提(任意・1〜2文。カード全体に表示)"},
        "questions": {"type": "array", "minItems": 1, "maxItems": 3,
            "description": "ユーザーに尋ねる質問(1〜3個)",
            "items": {"type": "object", "properties": {
                "header": {"type": "string", "description": "短いラベル(例: 認証方式)"},
                "question": {"type": "string", "description": "具体的な質問文"},
                "multiSelect": {"type": "boolean",
                                "description": "複数選択を許すなら true(チェックボックス表示)"},
                "options": {"type": "array", "minItems": 2, "maxItems": 4,
                    "description": "2〜4個の選択肢",
                    "items": {"type": "object", "properties": {
                        "label": {"type": "string", "description": "短い見出し(1〜5語)"},
                        "description": {"type": "string", "description": "選ぶと何が起きるか(1文)"},
                        "recommended": {"type": "boolean", "description": "推奨ならtrue(各質問で最大1つ)"}},
                        "required": ["label"]}}},
                "required": ["question", "options"]}}},
        "required": ["questions"]}}}
_T_SUMM = {"type": "function", "function": {
    "name": "summarize_path",
    "description": "フォルダ(または1ファイル)配下の全ファイルを map-reduce で要約・集約する。"
                   "多数ファイルの概要把握や横断要約に使う(個別 read より効率的)。",
    "parameters": {"type": "object", "properties": {
        "path": {"type": "string", "description": "作業フォルダからの相対パス(フォルダ or ファイル)"},
        "instruction": {"type": "string", "description": "要約の観点・依頼(任意)"}},
        "required": ["path"]}}}

READ_TOOLS = [_T_LIST, _T_READ, _T_GLOB, _T_GREP, _T_SUMM]
WRITE_TOOLS = [_T_WRITE, _T_EDIT, _T_CMD, _T_BG]
META_TOOLS = [_T_BGOUT, _T_BGSTOP]   # 確認不要のメタ操作(jobの出力取得・停止)
# 計画フェーズでも変更系ツールを提示する。原則は present_plan→承認→自動適用だが、
# モデルが計画を出さずに編集しようとした場合でも、差分つき確認カードで承認すれば適用できる
# (計画を出さないモデルでも「修正してくれない」状態に陥らないようにするため)。
PLAN_PHASE_TOOLS = READ_TOOLS + WRITE_TOOLS + META_TOOLS + [_T_TODO, _T_ASK, _T_PLAN]
EXEC_PHASE_TOOLS = READ_TOOLS + WRITE_TOOLS + META_TOOLS + [_T_TODO, _T_ASK]

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


def _norm_options(raw) -> list:
    """ask_user の選択肢を {label, description, recommended} に正規化。
    文字列・オブジェクトのどちらで来ても受け取れるようにする(弱いモデル対策)。"""
    if isinstance(raw, str):                      # 配列をJSON文字列で渡すモデルがある
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    if isinstance(raw, dict):                     # 単一選択肢をオブジェクトのまま渡す場合
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    out, rec_used = [], False
    for o in raw:
        if isinstance(o, dict):
            label = str(o.get("label") or o.get("text") or o.get("value") or o.get("title") or "").strip()
            desc = str(o.get("description") or o.get("desc") or o.get("detail") or "").strip()
            rec = bool(o.get("recommended") or o.get("recommend") or o.get("default"))
        else:
            label, desc, rec = str(o).strip(), "", False
        if not label:
            continue
        if rec and rec_used:        # 推奨は最大1つに制限
            rec = False
        rec_used = rec_used or rec
        out.append({"label": label, "description": desc, "recommended": rec})
        if len(out) >= 4:
            break
    return out


def _norm_questions(args: dict) -> list:
    """ask_user の引数を質問リストに正規化する。
    新形式(questions 配列)・旧形式(question/options)・JSON文字列などのゆらぎを吸収。
    返り値: [{header, question, multiSelect, options:[{label,description,recommended}]}]"""
    raw = args.get("questions")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = None
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, (list, tuple)) or not raw:
        # 旧形式 / フォールバック(単一質問)
        raw = [{"header": args.get("header"), "question": args.get("question"),
                "multiSelect": args.get("multiSelect"), "options": args.get("options")}]
    out = []
    for q in raw:
        if not isinstance(q, dict):
            q = {"question": str(q)}
        question = str(q.get("question") or q.get("text") or q.get("title") or "").strip()
        header = str(q.get("header") or q.get("name") or "").strip()
        multi = bool(q.get("multiSelect") or q.get("multi") or q.get("multiple"))
        options = _norm_options(q.get("options"))
        if not question and not options:
            continue
        out.append({"header": header, "question": question or "どれにしますか?",
                    "multiSelect": multi, "options": options})
        if len(out) >= 3:
            break
    return out


def _format_ask_result(questions: list, ans) -> str:
    """ユーザーの回答(質問ごとの選択ラベル配列 / 文字列 / None)を、モデル向けの
    分かりやすいテキストに整形する。選んだ選択肢の説明も併記して取り違えを防ぐ。"""
    if isinstance(ans, list):
        per = ans
    elif isinstance(ans, str) and ans.strip():
        per = [[ans.strip()]]      # 旧形式(単一回答)
    else:
        per = []
    lines = ["ユーザーの回答:"]
    for i, q in enumerate(questions):
        sel = per[i] if i < len(per) else []
        if isinstance(sel, str):
            sel = [sel]
        sel = [str(s).strip() for s in (sel or []) if str(s).strip()]
        head = q.get("header") or q.get("question") or f"質問{i + 1}"
        if not sel:
            lines.append(f"- {head}: (回答なし)")
            continue
        parts = []
        for s in sel:
            o = next((o for o in q["options"] if o["label"] == s), None)
            parts.append(f"{s}({o['description']})" if o and o.get("description") else s)
        lines.append(f"- {head}: " + " / ".join(parts))
    return "\n".join(lines)


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


def t_read_file(ws: Path, path: str, offset: int = 0, limit: Optional[int] = None) -> str:
    try:
        p = _safe_path(ws, path)
    except ValueError as e:
        return f"[エラー] {e}"
    if not p.exists() or not p.is_file():
        return f"[エラー] ファイルが存在しません: {path}"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"[エラー] 読み取り失敗: {e}"
    lines = text.splitlines()
    total = len(lines)
    try:
        start = max(int(offset or 0), 0)
    except (TypeError, ValueError):
        start = 0
    if start > 0:        # offset は1始まりの行番号(0/1=先頭)
        start -= 1
    try:
        n = int(limit) if limit else READ_DEFAULT_LINES
    except (TypeError, ValueError):
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
        return t_read_file(ws, args.get("path", ""), args.get("offset", 0), args.get("limit"))
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
        _pending[aid] = {"event": threading.Event(), "approved": False,
                         "answer": None, "scope": None}
    return aid


def resolve(action_id: str, approved: bool, scope: Optional[str] = None) -> bool:
    """承認/拒否を記録。scope='always' なら以後このセッションの編集を自動適用する。"""
    with _pending_lock:
        p = _pending.get(action_id)
    if not p:
        return False
    p["approved"] = bool(approved)
    p["scope"] = scope
    p["event"].set()
    return True


def resolve_answer(action_id: str, answer: str) -> bool:
    """ask_user への回答(自由記述/選択肢)を記録して待機側を起こす。"""
    with _pending_lock:
        p = _pending.get(action_id)
    if not p:
        return False
    p["answer"] = answer
    p["event"].set()
    return True


def wait_answer(action_id: str, timeout: float = CONFIRM_TIMEOUT) -> Optional[str]:
    """ask_user の回答待ち。回答文字列 / None(タイムアウト)。"""
    with _pending_lock:
        p = _pending.get(action_id)
    if not p:
        return None
    ok = p["event"].wait(timeout)
    with _pending_lock:
        p = _pending.pop(action_id, None)
    if not ok or p is None:
        return None
    return p.get("answer")


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


def wait_decision(action_id: str, timeout: float = CONFIRM_TIMEOUT):
    """承認待ち(scope付き)。(approved, scope) を返す。approved: True/False/None。"""
    with _pending_lock:
        p = _pending.get(action_id)
    if not p:
        return (None, None)
    ok = p["event"].wait(timeout)
    with _pending_lock:
        p = _pending.pop(action_id, None)
    if not ok or p is None:
        return (None, None)
    return (p["approved"], p.get("scope"))


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
    if name == "summarize_path":
        return {"path": args.get("path", "")}
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

# コーディング向け生成パラメータ(安定性重視・ツール呼び出し優先で think は付けない)
AGENT_TEMPERATURE = 0.2
AGENT_TOP_P = 0.9
AGENT_NUM_PREDICT = 8192   # 1ステップの最大出力。長い編集/差分が途中で切れないよう大きめ


def _gen_options(num_ctx: Optional[int] = None) -> dict:
    """エージェントの生成オプション。サンプリングはコーディング向けに固定し、
    num_ctx(コンテキスト長)だけは会話設定の値を反映する(0/None はモデル既定)。"""
    opt = {
        "temperature": AGENT_TEMPERATURE,
        "top_p": AGENT_TOP_P,
        "num_predict": AGENT_NUM_PREDICT,
    }
    if num_ctx:
        opt["num_ctx"] = int(num_ctx)
    return opt


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


def compact_ctx_with_model(model: str, messages: list, num_ctx: Optional[int] = None) -> bool:
    """モデルを使って文脈を圧縮(必要時のみ)。num_ctx を渡すと要約対象の取りこぼしを防ぐ。"""
    def summarizer(text: str) -> str:
        opt = {"num_predict": 400}
        if num_ctx:
            opt["num_ctx"] = int(num_ctx)
        try:
            r = _client().chat(model=model, messages=[
                {"role": "system", "content": "次の作業ログを、後で作業を再開できるよう日本語で簡潔に要約してください。"
                 "重要な決定・変更したファイル・実行結果・残タスクを箇条書きで。"},
                {"role": "user", "content": text}], options=opt)
            return getattr(r.message, "content", "") or ""
        except Exception:
            return ""
    return compact_ctx(messages, summarizer)


def _change_action(name: str, plan_mode: bool, phase: str, allow_changes: bool,
                   auto_accept_edits: bool = False) -> str:
    """変更系ツールの扱いを返す: 'block'(不可) / 'confirm'(差分つき確認) / 'apply'(自動適用)。
    計画モードで計画が未承認でも、ハードブロックせず1操作ずつ確認に回す
    (present_plan を出さないモデルでも修正を適用できるようにするため)。"""
    if not plan_mode and not allow_changes:
        return "block"               # 非計画モードで変更オフ → 読み取りのみ
    # セッションで「編集を自動適用(acceptEdits)」が選ばれていれば、ファイル編集は確認不要。
    # コマンド系(run_command/run_background)は安全のため引き続き確認する(Claude Code 同様)。
    if auto_accept_edits and name not in CONFIRM_IN_EXEC:
        return "apply"
    if phase != "execute":
        return "confirm"             # 計画未承認 → 個別に差分つきで確認
    if not plan_mode:
        return "confirm"             # 非計画モード(変更許可) → 毎回確認
    if name in CONFIRM_IN_EXEC:
        return "confirm"             # 計画承認後でもコマンド系は確認
    return "apply"                   # 計画承認後のファイル編集 → 自動適用


def _result_status(result: str) -> str:
    """ツール結果の文字列から表示ステータスを判定([エラー] 始まりは失敗扱い)。"""
    return "error" if str(result or "").lstrip().startswith("[エラー]") else "ok"


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
               allow_changes: bool, plan_mode: bool = True,
               num_ctx: Optional[int] = None,
               auto_accept_edits: bool = False) -> Iterator[dict]:
    """
    エージェントを1依頼ぶん実行し、イベントを順次 yield する。
    イベント type:
      thinking / assistant_delta / tool_call / tool_result / confirm / plan / todos / done / max_steps / error
    plan_mode=True のときは「調査→present_plan→承認→実行」。
    各ステップの本文は逐次ストリーミング(assistant_delta)で流す。
    """
    ws = Path(workspace).resolve()
    client = _client()
    options = _gen_options(num_ctx)   # コーディング向け生成設定(num_ctx は会話設定を反映)
    phase = "plan" if plan_mode else "execute"
    investigated = False     # 調査(読み取り)系ツールを使ったか
    ask_redirected = False   # 「調査前の聞き返し」を一度リダイレクトしたか
    did_attempt_change = False  # 変更系ツールを一度でも呼んだか
    apply_nudged = False        # 「貼っただけで未適用」の催促を送ったか(1依頼1回)
    # 直近のユーザー依頼に「変更してほしい」意図があるか(安全網の発火条件に使う)
    wants_change = bool(_CHANGE_INTENT.search(
        next((str(m.get("content") or "") for m in reversed(messages)
              if m.get("role") == "user"), "")))

    for _ in range(MAX_STEPS):
        # 実行の途中でも文脈が大きくなったら自動圧縮(溢れ防止。Claude同様)
        try:
            if _ctx_chars(messages) > CTX_CHAR_LIMIT and compact_ctx_with_model(model, messages, num_ctx):
                log.info("文脈を自動圧縮しました(実行中)")
        except Exception:
            log.exception("実行中の文脈圧縮に失敗(無視して続行)")
        tools = PLAN_PHASE_TOOLS if phase == "plan" else EXEC_PHASE_TOOLS
        # --- 1ステップ生成(逐次ストリーミング。失敗時は非ストリームにフォールバック) ---
        content_parts: list[str] = []
        tool_calls: list = []
        streamed = False
        try:
            for chunk in client.chat(model=model, messages=messages, tools=tools,
                                     stream=True, options=options):
                cm = getattr(chunk, "message", None)
                if cm is None:
                    continue
                th = getattr(cm, "thinking", None)
                if th:
                    yield {"type": "thinking", "text": th}   # 思考は表示用に流す(保存はしない)
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
                resp = client.chat(model=model, messages=messages, tools=tools, options=options)
            except Exception as e2:
                emsg = str(e2)
                log.warning("agent 生成失敗: %s", emsg)
                if "tool" in emsg.lower():
                    emsg = (f"{emsg}\n(このモデルはツール呼び出しに未対応かもしれません。"
                            "qwen3 等のツール対応モデルをお試しください)")
                yield {"type": "error", "error": emsg}
                return
            cm = resp.message
            th = getattr(cm, "thinking", None)
            if th:
                yield {"type": "thinking", "text": th}
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
            # 安全網: 変更依頼なのにツールを呼ばず、コードをチャットに貼っただけで終わろうとした
            # 場合は、実ファイルへ適用するよう一度だけ促して継続する(計画モードでない/変更許可時)。
            if (wants_change and not did_attempt_change and not apply_nudged
                    and "```" in content and (allow_changes or plan_mode)):
                apply_nudged = True
                messages.append({"role": "user", "content": _APPLY_NUDGE})
                continue
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
            if name in READONLY or name == "summarize_path":
                investigated = True

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

            # --- ユーザーへの質問(選択式)。回答を待って続行 ---
            if name == "ask_user":
                # ガード: 調査もせず最初に聞き返すのは「質問の丸投げ」の可能性が高い。
                # 一度だけリダイレクトし、まず自分で調べる/答えるよう促す(無限ループは防ぐ)。
                if not investigated and not ask_redirected:
                    ask_redirected = True
                    result = ("[ガイド] まだ何も調査していません。ユーザーは多くの場合『答え』を求めています。"
                              "聞き返す前に list_files / read_file / grep などで調べ、"
                              "使い方・調べもの・原因調査の質問なら推測せず自分で結論を答えてください。"
                              "本当に作業方針が分岐して結果が変わる場合に限り ask_user を使ってください。")
                    yield {"type": "tool_result", "name": "ask_user", "status": "redirected", "result": result}
                    messages.append({"role": "tool", "content": result, "tool_name": name})
                    continue
                questions = _norm_questions(args)
                context = str(args.get("context") or "").strip()
                aid = new_pending()
                ev = {"type": "ask", "action_id": aid, "questions": questions}
                if context:
                    ev["context"] = context
                yield ev
                ans = wait_answer(aid)   # 質問ごとの選択ラベル配列 / 文字列 / None
                result = _format_ask_result(questions, ans)
                yield {"type": "tool_result", "name": "ask_user", "status": "ok", "result": result}
                messages.append({"role": "tool", "content": result, "tool_name": name})
                continue

            yield {"type": "tool_call", "name": name, "args": _preview_args(name, args)}

            # --- バックグラウンドjobの出力取得・停止(確認不要のメタ操作) ---
            if name in META:
                result = dispatch(ws, name, args)
                yield {"type": "tool_result", "name": name,
                       "status": _result_status(result), "result": result}
                messages.append({"role": "tool", "content": str(result), "tool_name": name})
                continue

            # --- 多数ファイルの map-reduce 要約(読み取りのみ・確認不要) ---
            if name == "summarize_path":
                rel = (args.get("path") or ".").strip() or "."
                instr = (args.get("instruction") or "").strip()
                try:
                    base = _safe_path(ws, rel)
                    from . import summarize as _summ, rag as _rag
                    from .defaults import get_defaults as _gd
                    sfiles = _rag.scan_files([str(base)])
                    if not sfiles:
                        result = "[対象ファイルがありません]"
                    else:
                        mm = (_gd().get("summarize_map_model") or "").strip() or None
                        if mm == model:
                            mm = None
                        fn = _summ.model_summarize_fn(model, instr, map_model=mm)
                        result = _summ.run_summarize(sfiles, instr, fn) or "(要約できませんでした)"
                except Exception as e:
                    result = f"[エラー] {e}"
                yield {"type": "tool_result", "name": name,
                       "status": _result_status(result), "result": result}
                messages.append({"role": "tool", "content": str(result), "tool_name": name})
                continue

            if name in MUTATING:
                did_attempt_change = True   # 変更を試みた(=安全網の催促は不要)
                action = _change_action(name, plan_mode, phase, allow_changes, auto_accept_edits)
                if action == "block":
                    result = ("[変更は許可されていません。画面の『変更を許可』をオンにすると、"
                              "承認のうえで変更・実行できます(読み取りは可能です)]")
                    yield {"type": "tool_result", "name": name, "status": "blocked", "result": result}
                elif action == "confirm":
                    # 差分つきで確認カードを出し、承認されたら適用(計画未承認でもここで適用可能)
                    detail = _action_detail(ws, name, args)
                    aid = new_pending()
                    yield {"type": "confirm", "action_id": aid, "name": name, **detail}
                    decision, scope = wait_decision(aid)
                    # 「以後自動適用」が選ばれたら、このセッションのファイル編集は確認を省く
                    if decision is True and scope == "always":
                        auto_accept_edits = True
                    if decision is True:
                        result = dispatch(ws, name, args)
                        ev = {"type": "tool_result", "name": name,
                              "status": _result_status(result), "result": result}
                        if detail.get("diff"):
                            ev["diff"] = detail["diff"]      # 履歴に残し再読込でも差分が見えるように
                        if detail.get("path"):
                            ev["path"] = detail["path"]
                        yield ev
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
                    ev = {"type": "tool_result", "name": name,
                          "status": _result_status(result), "result": result}
                    if detail.get("diff"):
                        ev["diff"] = detail["diff"]
                    if detail.get("path"):
                        ev["path"] = detail["path"]
                    yield ev
            else:
                result = dispatch(ws, name, args)
                yield {"type": "tool_result", "name": name,
                       "status": _result_status(result), "result": result}

            messages.append({"role": "tool", "content": str(result), "tool_name": name})

    yield {"type": "max_steps"}
