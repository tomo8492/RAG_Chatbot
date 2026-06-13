"""agent の定数・ツールスキーマ・システムプロンプト(leaf データ層)。

関数を持たない純データ。re のみ依存。_impl から取り込まれる。
"""

import re

MAX_STEPS = 40
MAX_VERIFY_ROUNDS = 3     # 自律検証ループ(変更→テスト→失敗なら修正→再検証)の最大回数
CMD_TIMEOUT = 120
CONFIRM_TIMEOUT = 600     # 承認待ちの最大秒数
MAX_GREP_FILE = 2_000_000  # grep で読むファイルの上限(2MB)
READ_DEFAULT_LINES = 800   # read_file の既定の読み取り行数
READ_CHAR_CAP = 20000      # read_file 1回の最大文字数(安全上限)
CTX_CHAR_LIMIT = 60000     # 文脈の合計文字数がこれを超えたら圧縮
# read_file がテキスト以外も読めるように(Claude の Read 相当)
_DOC_EXTS = {".pdf", ".docx", ".xlsx", ".pptx"}   # loaders で本文抽出
_IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}

SYSTEM_PROMPT = """あなたは優秀なソフトウェアエンジニアのエージェントです。
指定された「作業フォルダ」の中だけで、ユーザーの依頼を達成します。

【ツール】
- 調査(読み取り): list_files / read_file / glob(ファイル名検索) / grep(内容検索) / summarize_path(多数ファイルの一括要約)
- 調査の委譲: explore(読み取り専用の調査サブエージェントに横断調査を任せ、要約だけ受け取る。本体の文脈を節約)
- 変更: write_file(新規作成・全文上書き) / edit_file(既存ファイルの一部置換) / run_command(短時間コマンド)
- 長時間処理: run_background(devサーバ等。job_idを返す) / command_output(出力確認) / stop_command(停止)
- 進捗管理: todo_write(タスクのチェックリストを更新。多段作業で活用)
- メモリ: remember(プロジェクトの規約・前提・学んだ注意点を CLAUDE.md に追記し、次回以降も覚える)
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

【変更後の検証(重要・Claude流)】
- 変更したら可能な範囲で自分で動作を確かめる。まず設定(package.json の scripts / pyproject.toml /
  Makefile 等)を read_file・grep で調べ、テスト/ビルド/型チェック/lint があれば run_command で実行する
  (例: pytest / npm test / npm run build / tsc --noEmit / ruff / go build)。
- 失敗したら出力を読んで原因を直し、再実行する。緑になるまで(または原因が外部要因と判明するまで)繰り返す。
- 重い検証が難しいときは、せめて構文だけでも確かめる(例: python -m py_compile <file> / node --check <file>)。
- 検証手段が無い・不明なときは無理に実行せず、確認方法を1〜2行で提案するに留める。
- 変更後はシステム側が自動で検証(テスト/ビルド)を走らせる場合がある。失敗が差し戻されたら、
  その出力を読んで原因のファイルを直し、再度完了させること(同じ修正の繰り返しは避ける)。

【画像(スクショ)が添付されたとき】
- エラー画面・ログ・UI・図のスクリーンショットが添付されたら、それを読み取って調査・修正に使う。
  例: エラーメッセージの文言を読んで原因ファイルを grep → 該当箇所を edit_file で修正。
- 画像から読み取った文言・数値・手順は、推測で補わず見えるとおりに扱う。

【メモリ(remember)の使いどころ】
- ユーザーが「覚えておいて」と言ったとき、または「ビルド/テストのコマンド」「命名規則」「環境固有の注意」
  など"次も役立つ恒久的な事実"を見つけたときに remember で CLAUDE.md に1行記録する。
- 一時的・自明・その場限りのことは記録しない(メモの肥大化を避ける)。

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
    r"(変更|修正|直し|直す|実装|反映|適用|追加|作成|作っ|生成|実施|置換|置き換|書き換|書い|"
    r"削除|消去|リファクタ|対応して|してください|して下さい|"
    r"fix|change|implement|apply|refactor|add|create|update|edit|write|delete|remove)",
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
    "description": "指定ファイルの内容を読み取る(テキストに加え PDF/Word/Excel/PowerPoint も本文抽出して読める)。"
                   "大きいファイルは offset(開始行)/limit(行数)で続きも読める。画像は依頼に添付して見せる",
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
    "description": "既存ファイルの一部を置換する。old_string は一意に決まるよう十分な文脈を含めること。"
                   "同じファイルの複数箇所をまとめて直すときは edits 配列を使うと、1回の承認・"
                   "原子的な書き込みで適用できる(1件でも不一致なら全体を中止)。",
    "parameters": {"type": "object", "properties": {
        "path": {"type": "string", "description": "作業フォルダからの相対パス"},
        "old_string": {"type": "string", "description": "置換前の文字列(現在のファイルに存在する内容)。単一編集のとき指定"},
        "new_string": {"type": "string", "description": "置換後の文字列(単一編集のとき指定)"},
        "replace_all": {"type": "boolean", "description": "すべての一致を置換する場合 true(任意)"},
        "edits": {"type": "array", "description": "複数箇所をまとめて置換する場合の編集リスト(任意)。"
                  "指定時は old_string/new_string より優先",
                  "items": {"type": "object", "properties": {
                      "old_string": {"type": "string"},
                      "new_string": {"type": "string"},
                      "replace_all": {"type": "boolean"}},
                      "required": ["old_string", "new_string"]}}},
        "required": ["path"]}}}
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

_T_REMEMBER = {"type": "function", "function": {
    "name": "remember",
    "description": "プロジェクトの規約・前提・学んだ注意点を CLAUDE.md に追記し、次回以降も覚えておく"
                   "(例: ビルドは `npm run build`、テストは `pytest -q`、命名規則 等)。恒久的に有用な事実だけを簡潔に。",
    "parameters": {"type": "object", "properties": {
        "note": {"type": "string", "description": "記録する1行メモ(簡潔に)"}},
        "required": ["note"]}}}

_T_EXPLORE = {"type": "function", "function": {
    "name": "explore",
    "description": "読み取り専用の調査サブエージェントに調査を委譲し、結果の要約だけを受け取る"
                   "(本体の文脈を汚さず、横断的な調査・現状把握を任せる)。例: 認証まわりの実装と流れを調べて",
    "parameters": {"type": "object", "properties": {
        "task": {"type": "string", "description": "調査してほしい内容(具体的に)"}},
        "required": ["task"]}}}

_T_VERIFY = {"type": "function", "function": {
    "name": "verify",
    "description": "設定済み(または自動検出した)検証コマンド(テスト/ビルド/型/lint)を実行し、"
                   "合否と出力を得る。変更後に自分で動作確認したいときに使う"
                   "(任意のコマンドを実行したい場合は run_command を使う)。",
    "parameters": {"type": "object", "properties": {}, "required": []}}}

READ_TOOLS = [_T_LIST, _T_READ, _T_GLOB, _T_GREP, _T_SUMM]
WRITE_TOOLS = [_T_WRITE, _T_EDIT, _T_CMD, _T_BG]
META_TOOLS = [_T_BGOUT, _T_BGSTOP, _T_REMEMBER]   # 確認不要のメタ操作(job出力/停止・メモ追記)
# 読み取り専用の調査サブエージェントに渡すツール(変更系は持たせない)
SUBAGENT_TOOLS = [_T_LIST, _T_READ, _T_GLOB, _T_GREP]
SUBAGENT_MAX_STEPS = 12
SUBAGENT_RESULT_CAP = 6000   # 調査サブエージェントが1ツール結果として取り込む最大文字数(文脈膨張を防ぐ)
SUBAGENT_SYSTEM = (
    "あなたは読み取り専用の調査サブエージェントです。与えられた調査タスクについて、"
    "list_files / read_file / glob / grep だけを使って作業フォルダ内を調べ、事実に基づく"
    "簡潔な調査結果を日本語で返します。ファイルの変更やコマンド実行はできません。"
    "十分に調べたら、関係するファイルと該当箇所(`相対パス:行`)・わかったことを箇条書きで"
    "まとめて返してください(憶測は避け、確認できたことだけを書く)。"
)
# 計画フェーズでも変更系ツールを提示する。原則は present_plan→承認→自動適用だが、
# モデルが計画を出さずに編集しようとした場合でも、差分つき確認カードで承認すれば適用できる
# (計画を出さないモデルでも「修正してくれない」状態に陥らないようにするため)。
PLAN_PHASE_TOOLS = READ_TOOLS + WRITE_TOOLS + META_TOOLS + [_T_TODO, _T_ASK, _T_PLAN, _T_EXPLORE]
EXEC_PHASE_TOOLS = READ_TOOLS + WRITE_TOOLS + META_TOOLS + [_T_TODO, _T_ASK, _T_EXPLORE]

READONLY = {"list_files", "read_file", "glob", "grep"}
MUTATING = {"write_file", "edit_file", "run_command", "run_background"}
META = {"command_output", "stop_command"}        # 常に許可・確認不要
CONFIRM_IN_EXEC = {"run_command", "run_background"}   # 計画承認後でも確認する重要操作

IGNORE_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode", "dist", "build"}

# プロジェクト指示(CLAUDE.md 等)。作業フォルダ直下にあれば自動で読み込む。
PROJECT_FILES = ["CLAUDE.md", "AGENTS.md", ".claude/CLAUDE.md"]


# アンダースコア定数も含め、re と dunder 以外を公開(取り込み側で利用)
__all__ = [_k for _k in dict(globals()) if not _k.startswith("__") and _k != "re"]
