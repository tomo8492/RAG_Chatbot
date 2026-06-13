# CLAUDE.md — Claude Code 作業規約(このリポジトリ)

社内文書アシスタント(RAG Chatbot)。FastAPI + Ollama + ChromaDB の完全ローカル構成。

## 最重要ルール: アーキテクチャ図への反映(必須)

**機能の追加・変更・削除を行ったら、同じ作業の中で必ずアーキテクチャ図を更新すること。**
コミットを分けてもよいが、図の更新を伴わずに機能変更を「完了」と見なさない。

更新対象と、更新が必要になる変化:

- **`docs/architecture-map.html`**(全体構成のSVG図)— 以下のいずれかが変わったら更新:
  - モジュール(`app/*.py` 等)の追加・削除・役割変更 → `NODES` と `INFO`(概要 `d` /
    詳細 `pts` / 主シンボル `t`)
  - モジュール間の依存関係の変化 → `EDGES`
  - 主要な処理の流れ(質問応答 / 索引構築 / Code エージェント / 手順ビューア 等)の変化 →
    `FLOWS`
- **`docs/flows-interactive.html`**(処理フローの図)— チャット/取り込み/エージェントの
  **処理ステップ・分岐・異常系**が変わったら、対応するノード・エッジ・ノード詳細を更新。

更新後の検証(どちらの図も HTML 内に inline `<script>` を持つ):
```bash
# 構文チェック
python3 -c "import re;[open(f'/tmp/_a{i}.js','w').write(s) for i,s in enumerate(re.findall(r'<script>(.*?)</script>', open('docs/architecture-map.html').read(), re.S))]"
for f in /tmp/_a*.js; do node --check "$f"; done
# mermaid を含む flows-interactive.html は、図定義が mermaid v11 で parse 可能かも確認する
```
可能なら jsdom で「ノード数・エッジ・フロー・パネル描画・参照整合(NODES↔INFO↔EDGES↔FLOWS)」
を確認する(本セッションで使った検証手順を踏襲)。図は単体HTMLでアプリ挙動に影響しないが、
**壊れた図を放置しない**(リンク切れノード・未定義参照を残さない)。

## 開発ワークフロー(コミット前・CONTRIBUTING.md と同じ)
```bash
ruff check app/ tests/ evalkit/        # lint(必須・CIブロッキング)
python tests/run_all.py                # テスト(必須・CIブロッキング)
mypy app/                              # 型(情報)
# フロント(JSを変更したとき):
cat $(grep -oE '/static/js/[a-z_]+\.js' app/static/index.html | sed 's#/static#app/static#') > /tmp/_b.js
npx eslint@9 /tmp/_b.js
```
- テストは pytest 非依存の独自ランナー。各 `tests/test_*.py` は `__main__` で単体実行でき、
  `tests/run_all.py` が全ファイルを回す。新規テストも同形式に合わせる。
- 機能を足したら**テストも足す**(挙動を変えない改修は先にテスト)。

## 設計の要点(詳細は docs/architecture.md / architecture-map.html)
- **レイヤを守る**: `routes/`(薄いHTTP)→ `services/`(業務)→ エンジン(`rag`/`agent`/`llm`/
  `export`/`procedure` 等)。ルートに業務ロジックを書かない。
- **オフライン厳守**: 社外送信を増やさない(`LAN_ONLY` / Ollama ローカル)。
- **例外を握りつぶさない**: `except` では最低 `log.debug(..., exc_info=True)`。
- **単一プロセス前提**(`--workers=1`)。共有状態はロックで保護。
- 図/ドキュメントの導線は **README.md「🧩 構成」** に集約。

## ブランチ
- 指定された機能ブランチで開発し、CI(ruff・テスト・ESLint)を緑にしてから main へPR/マージ。
