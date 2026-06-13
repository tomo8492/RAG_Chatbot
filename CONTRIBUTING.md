# 開発ガイド (CONTRIBUTING)

社内文書アシスタント(RAG Chatbot)の開発手順と規約。アーキテクチャの全体像は
[docs/architecture.md](docs/architecture.md)、改善計画は [docs/code-roadmap.md](docs/code-roadmap.md) を参照。

## セットアップ
- Python **3.11+**
- 本番フル機能: `pip install -r requirements.txt`(LLM は Ollama、`ollama serve` が必要)
- 開発/CI の最小構成: `pip install -r requirements-test.txt`(重い ML 依存は除外。
  無い環境ではテストが自動 skip される)

## 開発ワークフロー(コミット前に実行)
```bash
ruff check app/ tests/ evalkit/        # Python lint(必須・CIブロッキング)
python tests/run_all.py                 # テスト(必須・CIブロッキング)
mypy app/                               # 型(情報。CIは非ブロッキング)
# フロント(任意・CIでも実行): 8つのJSは「共有グローバルスコープ」なので結合して検査
cat $(grep -oE '/static/js/[a-z_]+\.js' app/static/index.html | sed 's#/static#app/static#') > _eslint_bundle.js
npx eslint@9 _eslint_bundle.js          # no-undef(未定義参照/タイプミス検出)
```
- テストは **pytest 非依存の独自ランナー**。各 `tests/test_*.py` は `__main__` で単体実行でき、
  `tests/run_all.py` が全ファイルをサブプロセスで回す。新規テストも同じ形式に合わせる。
- フロントは現状 **クラシック script を順に読み込む**(`app/static/js/*.js`)。JS を追加したら
  `index.html` の `<script>` にも順序を意識して追加する(結合 ESLint がその順序を使う)。
- CI(`.github/workflows/ci.yml`)が push/PR で **ruff・テスト・ESLint(no-undef)** を自動実行する
  (mypy は情報)。

## コーディング規約
- **オフライン厳守**: 社外送信を増やさない(`LAN_ONLY` / Ollama ローカル)。新規の外部通信は入れない。
- **挙動を変えない改修**: リファクタは外部挙動(HTTP API・公開関数)を不変に。**先にテストを足してから**動かす。
- **レイヤを守る**: `routes/`(薄い HTTP)→ `services/`(業務ロジック)→ エンジン(`rag`/`agent`/`llm`/`export` 等)。
  ルートに業務ロジックを書かない。
- **例外は握りつぶさない**: `except` では最低でも `log.debug(..., exc_info=True)` を残す
  (サイレント握りつぶし禁止)。利用者向けは簡潔に、ログには文脈(対象ID等)を。
- **型注釈**を付ける(戻り値型は特に)。`ruff` の既定ルール(E4/E7/E9+F)を緑に保つ。
- **並行性**: 共有状態はロックで保護。単一プロセス前提(`uvicorn --workers=1`)を崩さない
  (詳細は architecture.md の並行性モデル)。
- **アーキテクチャ図を更新する(必須)**: 機能の追加・変更・削除でモジュール構成・依存・
  処理フローが変わったら、同じ作業の中で [docs/architecture-map.html](docs/architecture-map.html)
  (`NODES`/`EDGES`/`FLOWS`/`INFO`)を更新する。処理ステップ・分岐が変わるときは
  [docs/flows-interactive.html](docs/flows-interactive.html) も更新。更新後は inline script を
  `node --check`(mermaid図は parse 可否)で検証し、未定義参照を残さない。

## ブランチ / コミット
- 機能ブランチで開発し、明確なコミットメッセージ(日本語可)を付ける。
- 1コミット=1関心事。巨大ファイルは「抽出 → 委譲」で段階的に薄くする。

## 機微情報
- `data/`(DB・ベクトル・鍵・アップロード)と `.env` は **gitignore 済み=コミットしない**。
- 実データ(社内規定・帳票等)はリポジトリに入れない。テストは匿名化した最小例に限る。

## ディレクトリ早見表
| パス | 役割 |
|---|---|
| `app/main.py` | FastAPI 本体・ミドルウェア(LANガード/観測性)・chat/agent/static ルート |
| `app/routes/` | ドメイン別の薄い HTTP ルート |
| `app/services/` | 業務ロジック(HTTP 非依存) |
| `app/agent/` | コーディングエージェント(constants/tools/approvals/context/_impl/facade) |
| `app/rag.py` `retrieval.py` `embeddings.py` | 検索 / RAG(埋め込みは multilingual-e5-base) |
| `app/export.py` | 回答のファイル出力(md/txt/html/csv/pdf/docx/xlsx/pptx) |
| `app/static/` | フロントエンド(HTML/CSS/JS) |
| `tests/` | 単体テスト(独自ランナー) |
| `evalkit/` | 検索精度の評価 |
