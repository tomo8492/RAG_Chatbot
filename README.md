# 社内文書アシスタント (RAG Chatbot)

フォルダ内の文書(PDF / Word / Excel / PowerPoint / テキスト)をもとに回答する、
**完全ローカル** の RAG チャットボットです。Ollama によるローカル LLM を使うため、
**社外へのデータ送信はありません**。

ブラウザUI(FastAPI + 自作フロント)なので、**同じネットワークの他のPCからも利用できます**。

> 旧版(Tkinterデスクトップ版)は `legacy/rag_chat_tkinter.py` に保存してあります。

---

## ✨ 主な機能

| 機能 | 説明 |
|---|---|
| 📁 参照資料フォルダの選択 | サーバ(このアプリを動かすPC)のフォルダをブラウザから選んでインデックス化。複数フォルダ対応 |
| 🌐 他PCからアクセス | `0.0.0.0` で待受。社内LANの他PCのブラウザから利用可能 |
| 💬 複数会話・履歴保存 | Claude風に会話を複数保持。サイドバーから切替・リネーム・削除 |
| ⏩ ストリーミング・停止・再生成 | 回答を逐次表示。途中停止、もう一度生成し直し |
| 🧠 工数(思考の深さ) | 推論モデル(qwen3等)の thinking を「最小〜最大」で調整。思考過程も表示 |
| 📝 Markdown / コード表示 | 表・箇条書き・コードを整形表示。コードと回答はコピー可能 |
| 📎 ファイル添付 | チャットに直接ファイルを添付し、その内容について質問 |
| 🖼 画像コピペ・D&D | スクショを **Ctrl+V で貼り付け** / 画像・ファイルを**ドラッグ&ドロップ**。画像はVisionモデルで内容を理解 |
| 🔎 出典表示 | 回答の根拠にした資料ファイル・場所を明示 |
| 📄 ファイル出力 | 回答を **Word / Excel / PowerPoint / HTML / テキスト / Markdown** で保存。コードは `.bas` 等で個別ダウンロード |
| ⚙ 細かな設定 | モデル / 温度 / 回答長 / top_k / コンテキスト長 / チャンク等を調整 |
| ⚡ チャット欄クイック設定 | チャット画面から モデル・工数・回答長・参照件数 を即変更 |
| 🔐 簡易パスワード認証 | LAN公開時の保護。`.env` でON/OFF |
| 🌓 ライト / ダークテーマ | 右上のボタンで切替 |

---

## 🛠 必要なもの

1. **Python 3.10 以上**(推奨 3.11 / 3.12)
2. **Ollama**(ローカルLLM実行環境) … https://ollama.com/download
3. 文書を入れたフォルダ

---

## 📦 セットアップ

### 1. Ollama の準備

```bash
# Ollama をインストール後、使うモデルを取得(例)
ollama pull qwen3:8b        # 軽量〜中。thinking対応
# 高性能機なら:  ollama pull qwen3:32b
```

Ollama は通常 `http://localhost:11434` で自動起動します。

### 2. このアプリの準備

```bash
# 依存パッケージのインストール(初回は torch 等のDLで時間がかかります)
pip install -r requirements.txt
```

### 3. 設定ファイル(任意)

```bash
cp .env.example .env
# .env を編集(モデル名・パスワード・ポート等)
```

`.env` を作らなくても既定値で動作します。主な設定:

| 変数 | 既定 | 説明 |
|---|---|---|
| `HOST` | `0.0.0.0` | `0.0.0.0`=他PCから可 / `127.0.0.1`=自分だけ |
| `LAN_ONLY` | `true` | ローカルLAN/このPC以外(インターネット側のグローバルIP)からのアクセスを拒否 |
| `PORT` | `8000` | ポート番号 |
| `CHAT_PASSWORD` | (空) | 設定するとログイン必須。**他PC公開時は必ず設定** |
| `CHAT_MODEL` | `qwen3-32b:latest` | 既定の生成モデル |
| `VISION_MODEL` | `qwen2.5vl` | 画像(スクショ)付き質問で使う Vision モデル(要 `ollama pull`) |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama の場所 |
| `EMBED_MODEL` | `intfloat/multilingual-e5-small` | 埋め込みモデル(日本語対応) |

---

## ▶ 起動

```bash
python run.py
```

### Windows の場合(かんたん)

- **`start.bat` をダブルクリック**すると、必要なパッケージのインストールと起動をまとめて行います。
- または、フォルダで「ターミナルを開く」→ `python run.py`(`python` が無ければ `py run.py`)。
- VSCode / Cursor を使う場合は、`F5`(実行)で常に `run.py` が起動するよう設定済みです。

> ⚠️ IDEの「実行」ボタンは、**開いているタブのファイル**を実行します。出力パネルや
> `run.py` 以外のタブを開いたまま実行すると `FileNotFoundError` になります。
> その場合は `start.bat` か、ターミナルで `python run.py` を使ってください。

起動するとターミナルにアクセスURLが表示されます:

```
========================================================
 社内文書アシスタント
========================================================
 このPC      : http://localhost:8000
 他のPCから  : http://192.168.x.x:8000
 認証        : 有効
 Ollama      : http://localhost:11434 / モデル qwen3-32b:latest
========================================================
```

ブラウザで表示されたURLを開いてください。

---

## 💻 ブラウザを使わずターミナルだけで試す

動作確認用に、ブラウザ・Webサーバ不要でチャットできる `chat_cli.py` を同梱しています。

```bash
python chat_cli.py                       # 通常チャット(既定モデル)
python chat_cli.py --model qwen3-32b:latest   # モデル指定
python chat_cli.py --folder "C:\docs"    # フォルダを取り込んでRAGチャット
python chat_cli.py --index <ID>          # 既存インデックスでRAGチャット
```

会話中のコマンド: `/help` `/model 名前` `/effort off|low|medium|high|max`
`/topk 数` `/len 数` `/folder パス` `/index` `/reset` `/exit`

前提: `pip install -r requirements.txt` 済み・Ollama 起動済み・モデル取得済み。

---

## 🤖 コーディングエージェント(ターミナル / Claude Code 風)

指定した作業フォルダの中で、ローカルLLMが **ファイル作成・編集・コマンド実行**を行い、
プログラム作成を手伝います(この機能はアプリを動かす実機の上で使います)。

```bash
python code_agent.py --folder "C:\Users\220557\Documents\myproject"
python code_agent.py --folder . --model qwen3-32b:latest
python code_agent.py --folder ./proj --auto      # コマンドを確認なしで実行
```

- 操作はすべて **作業フォルダ内に限定**(外には書き込めません)
- コマンド実行は既定で **実行前に確認**(`--auto` で自動実行)
- 依頼を入力 → エージェントが作業。`/reset` 履歴クリア / `/exit` 終了
- ツール対応モデル(qwen3 等)が必要。精度は Claude 本体ほどではありません

---

## 🌐 他のPCのブラウザから使う

1. アプリを動かすPCで `HOST=0.0.0.0`(既定)にする
2. 起動時に表示される `http://192.168.x.x:8000` を、他PCのブラウザで開く
3. つながらない場合は、**サーバPCのファイアウォール**でポート(既定8000)の受信を許可:
   - **Windows**: 「Windows Defender ファイアウォール」→ 受信の規則 → 新規 → ポート → TCP 8000 を許可
   - **Mac**: システム設定 → ネットワーク → ファイアウォール
4. **公開する場合は必ず `CHAT_PASSWORD` を設定**してください(同じLANの誰でもアクセスできてしまうため)

> 参照資料フォルダは「サーバPC上」のフォルダを選びます。各PCのローカルフォルダを使いたい場合は、
> チャットの📎添付機能でファイルを送ってください。

---

## 📖 使い方

1. **参照資料を登録**(任意): 左下「📁 参照資料」→「フォルダを選んで追加」→ サーバ上のフォルダを選択 → インデックス作成
2. **会話で資料を有効化**: 「参照資料」一覧で、その会話で使う資料にチェック
3. **質問する**: 下部の入力欄に入力して送信(Enterで送信 / Shift+Enterで改行)
4. **クイック設定**: 入力欄上部で「モデル / 工数 / 長さ / 参照件数」をその場で変更
5. **ファイル添付**: 📎ボタン、またはドラッグ&ドロップでファイルを添付して質問
6. **画像のコピペ**: スクショを **Ctrl+V** で貼り付け(または画像をD&D)→ 質問。画像理解には Vision モデルが必要(`ollama pull qwen2.5vl`)
6. **詳細設定**: 左下「⚙ 設定」で温度・回答長・チャンク等を細かく調整(新しい会話の既定値になります)
7. **回答を保存**: 各回答の下「⬇ 保存」から Word / Excel / PowerPoint / HTML / テキスト / Markdown で出力。
   コードブロックは右上の「⬇」で `.bas` 等の拡張子でダウンロード(「〜をExcelで作って」→ 回答をExcel保存、も可能)

入力対応形式: `.pdf .docx .xlsx .pptx .txt .md .csv .tsv .log .json`
出力対応形式: `.docx .xlsx .pptx .html .txt .md` + コード(`.bas .py .js .html .sql` など任意)

---

## 🧩 構成

```
RAG_Chatbot/
├── run.py                  # 起動スクリプト
├── requirements.txt
├── .env.example
├── app/
│   ├── main.py             # FastAPI 本体・APIルート・SSE生成
│   ├── config.py           # 設定(.env)
│   ├── db.py               # SQLite(会話・メッセージ・インデックス)
│   ├── auth.py             # 簡易パスワード認証
│   ├── llm.py              # Ollama生成(ストリーミング・thinking)
│   ├── embeddings.py       # 埋め込み(sentence-transformers / Ollama)
│   ├── rag.py              # ChromaDB によるRAG
│   ├── loaders.py          # 文書ローダ
│   ├── splitter.py         # テキスト分割
│   ├── fsbrowse.py         # サーバのフォルダ閲覧
│   ├── defaults.py         # 生成パラメータの既定値・マージ
│   └── static/             # フロントエンド(HTML/CSS/JS + 同梱ライブラリ)
├── legacy/
│   └── rag_chat_tkinter.py # 旧Tkinter版
└── data/                   # 実行時生成(DB・ベクトル・添付)※git管理外
```

技術: FastAPI / Uvicorn ・ Ollama ・ ChromaDB(永続) ・ sentence-transformers(多言語E5) ・
marked + highlight.js + DOMPurify(すべてローカル同梱、オフライン動作)

---

## 🔧 トラブルシューティング

| 症状 | 対処 |
|---|---|
| 「Ollama に接続できません」 | `ollama serve` が動いているか、`OLLAMA_HOST` を確認 |
| 「(モデルなし)」と出る | `ollama pull qwen3:8b` 等でモデルを取得 |
| 他PCから開けない | `HOST=0.0.0.0`か確認 + サーバPCのファイアウォールでポート許可 |
| 回答が資料を参照しない | 「参照資料」でその会話の資料にチェックが入っているか確認 |
| PDFが読めない | 画像のみのスキャンPDFはテキスト抽出不可(OCR未対応) |
| 初回起動が遅い | 埋め込みモデルの初回DL。2回目以降は高速 |

ログはすべて起動したターミナルに出力されます。

---

## 🔒 セキュリティ

- LLM・埋め込み・ベクトルDBはすべてローカル。**外部送信なし**
- 他PCに公開する場合は `CHAT_PASSWORD` を必ず設定
- フォルダ閲覧機能はサーバのファイルシステムを参照するため、信頼できるネットワークでのみ利用してください
