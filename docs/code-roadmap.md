# コード改善ロードマップ(技術的負債・保守性)

RAG_Chatbot の**コード全体**の今後の改善計画。機能追加ではなく、**保守性・テスト容易性・
品質ゲート**の底上げが目的。推測でなく、実コードを計測して根拠を付けた。

- 最終更新: 2026-06-06
- 調査方法: 全 `.py`(legacy除く 約8,900行)とフロント、依存・例外・ログ・型・テストを実測。
- 機能ロードマップ(規定/工場テーマ)は [roadmap.md](../roadmap.md) を参照。本書は**コード品質**側。

---

## 1. 現状評価

### 1.1 強み(壊さない・横展開する)

| 項目 | 根拠(実測) |
|---|---|
| モジュール分割が明確 | `app/` が loaders/splitter/retrieval/rag/llm/embeddings/export/summarize/db/auth/safety/config/ocr/postprocess/agent/main 等に責務分離 |
| テストが厚く全グリーン | **166 テスト関数 / 10 ファイルが全て成功**。重い依存(PyMuPDF・sentence-transformers)が無い環境では**自動 skip** = CI に載せやすい |
| 例外・ログの基本作法 | **bare `except:` ゼロ**、`print()` 不使用(logging 経由) |
| 型注釈の密度 | 関数 274 中 **232(約85%)に戻り値型** |
| DB 設計が堅実 | SQLite を WAL・**呼び出し毎接続**・書込みグローバルロック・`check_same_thread=False`・timeout=30。現行スケールに適切 |
| 認証が堅実 | パスワード/OCR APIキーとも **`hmac.compare_digest`(定数時間)**、署名Cookie(itsdangerous)+失効。LAN ガード/IP 許可も実装 |
| 評価基盤 | `evalkit/`(検索精度の実測)あり |

> 結論: **土台は健全**。大規模な作り直しは不要。負債は「品質ゲートの不在」と「一部の巨大モジュール」に集中。

### 1.2 改善余地(根拠付き)

| # | 課題 | 根拠(実測) | 影響 |
|---|---|---|---|
| C1 | **CI/品質ゲートが皆無** | `.github` なし。lint/format/型/pre-commit/pyproject **設定ファイルゼロ**。166テストは手動実行のみ | 回帰が自動検知されない。最大の機会損失 |
| C2 | **`main.py` が太い(1,228行/34ルート)** | `api_generate` ≈190行・`api_agent` ≈140行がルート内に業務ロジックを内包。main.py の**テストなし** | 変更が怖い・テスト不能 |
| C3 | **`agent.py` が神モジュール(1,343行)** | ツール(`t_*`)+承認ステートマシン(`new_pending/resolve/wait`)+差分プレビュー+文脈圧縮+エージェントループ(`run_stream`)が同居 | 認知負荷大・改修の波及 |
| C4 | **並行状態がモジュール変数** | `agent.py` の承認/バックグラウンドジョブ、`main.py` の要約/索引ワーカが**プロセス内 dict + スレッド**。`uvicorn --workers>1` で破綻 | 単一プロセス前提が暗黙。スケール時に事故 |
| C5 | **例外が広め・ログが希薄** | `except Exception` **92箇所**(広い except 計108)に対し logging 呼び出しは **7箇所**のみ | 失敗が静かに握りつぶされ得る/原因究明が困難 |
| C6 | **依存が再現性に弱い** | requirements は全て `>=` のみ・**ロックファイルなし**・Python版未指定 | ビルドが時期で変わる/供給網ドリフト |
| C7 | **フロントが単一巨大ファイル** | `app/static/js/app.js` **2,587行**(モジュール分割・ビルドなし) | 変更が局所化しづらい |
| C8 | **テスト空白域** | `main.py`(API契約)・`rag.retrieve`・`summarize`・`safety`・`fsbrowse` に専用テストなし。カバレッジ計測もなし | リファクタの安全網が一部欠落 |
| C9 | **大きい補助モジュール** | `export.py` 1,001行(形式別関数が密集) | 肥大化が続くと保守性低下 |
| C10 | **貢献者向けドキュメント不足** | README は利用者向け。アーキ概要/モジュール依存図/CONTRIBUTING なし | オンボーディングが属人化 |

---

## 2. 横断方針(改善時の鉄則)

1. **挙動を変えない** — リファクタは外部挙動不変。**先にテストを足してから**動かす(特に C2/C3)。
2. **品質ゲートを最初に** — 自動チェックが無い状態での大改修はしない(C1 をフェーズ0に置く理由)。
3. **小さく刻む** — 1PR=1関心事。巨大ファイルは「抽出 → 委譲」で段階的に薄くする。
4. **機能ロードマップを止めない** — コード改善は機能開発(規定/工場)と並行できる粒度にする。
5. **オフライン/LAN・セキュリティ水準を維持** — 既存の堅実な実装(認証・LANガード)を後退させない。

---

## 3. フェーズ別ロードマップ(ROI と低リスク順)

```
フェーズ0  品質ゲート整備        C1, C6      ← 最優先(低リスク・全体に波及)
フェーズ1  ルートを薄く+API試験  C2, C8      ← main.py から業務ロジックを service へ
フェーズ2  agent分割+並行性明文化 C3, C4      ← 神モジュール解体・状態管理の集約
フェーズ3  観測性と例外の健全化   C5          ← ログ拡充・広い except の精査
フェーズ4  フロント/補助/文書     C7, C9, C10 ← 長期保守性(分割・アーキ文書)
```

### フェーズ0 — 品質ゲート整備 【最優先 / リスク低】
- **GitHub Actions**: push/PR で `python tests/run_all.py` を実行(依存最小構成でも skip により緑になる)。
- **ruff** 導入(lint+format)。まず警告ベースで運用 → 段階的に強制。
- **mypy**(型注釈が既に約85% → 投資対効果が高い)。`app/` から段階適用。
- **依存の再現性**(C6): `pyproject.toml` 化 or ロックファイル(pip-tools/uv)、Python版明記、Dependabot/更新方針。
- 任意: pre-commit(ruff/mypy をコミット前に)。
- **Done**: PR ごとにテスト+lint が自動で回り、赤を弾ける。
- 規模: 中 / リスク: 低。

### フェーズ1 — ルートを薄くしてテスト可能に 【C2, C8】
- `main.py` の業務ロジックを **`app/services/` へ抽出**(例: `chat_service.generate(...)`, `agent_service`, `summarize_service`, `index_service`)。ルートは「検証→サービス呼び出し→整形/ストリーム」だけに。
- 抽出に合わせ **FastAPI `TestClient` で API 契約テスト**を追加(認証・会話CRUD・generate の SSE 形・エラー時)。`rag`/`llm` は fake で差し替え。
- `coverage` 計測を CI に追加(下限は緩く開始)。
- **Done**: `main.py` が薄くなり、主要 API にテストが付く。
- 規模: 大(段階分割可) / リスク: 中(テスト先行で低減)。

### フェーズ2 — `agent.py` 分割と並行性の明文化 【C3, C4】
- 神モジュールを責務で分割(例):
  - `agent/tools.py`(`t_*` 群)
  - `agent/approvals.py`(承認ステートマシン+共有状態。**ロックで保護**)
  - `agent/preview.py`(差分・プレビュー)
  - `agent/context.py`(`compact_ctx` 文脈圧縮)
  - `agent/loop.py`(`run_stream`)
- **並行モデルを明文化**(C4): バックグラウンドジョブ/承認の共有 dict を1箇所に集約しロック化。**「単一プロセス前提(`--workers=1`)」を README/コードに明記**。将来の多ワーカ対応は別途設計(状態を DB/キューへ)。
- **Done**: 1ファイル <~500行、共有状態の境界が明確、回帰テスト緑。
- 規模: 大 / リスク: 中。

### フェーズ3 — 観測性と例外処理の健全化 【C5】
- 広い `except Exception`(92) を棚卸し:握りつぶしを**例外種別の絞り込み+ログ必須**に。利用者向けには簡潔、ログには文脈(入力要約・対象ID)。
- 構造化ログ/リクエストID で生成・索引・要約の追跡性を上げる(現状ログ呼び出し7箇所)。
- **Done**: 失敗時に必ず痕跡が残る。サイレント握りつぶしゼロ。
- 規模: 中 / リスク: 低。

### フェーズ4 — フロント/補助モジュール/文書 【C7, C9, C10】
- フロント(C7): `app.js` を ES モジュールで関心事ごとに分割(チャット/コード/索引/要約/設定)。フレームワーク移行は不要、軽いモジュール化で十分。
- 補助(C9): `export.py` を形式別(`export/word.py` 等)へ分割(肥大が続く場合)。
- 文書(C10): `docs/architecture.md`(モジュール依存図・データフロー・並行モデル)と CONTRIBUTING。
- 規模: 中〜大 / リスク: 低。

---

## 4. クイックウィン(すぐ着手できる低リスク)
- GitHub Actions で `python tests/run_all.py` を回す(半日)。
- `ruff` を導入し format 統一(警告運用から)。
- 依存に Python 版明記 + ロックファイル化。
- `docs/architecture.md` の雛形を作り、本書とロードマップから相互リンク。

## 5. やらないこと(スコープ外・過剰回避)
- フロントのフレームワーク全面移行(React等)— 現状の規模では過剰。
- マイクロサービス化 / DB を即 PostgreSQL へ — LAN・単一拠点には不要。
- 認証基盤の作り直し — 現状の定数時間比較+署名Cookieで水準は十分。
- 大規模リライト全般 — 「抽出して薄くする」漸進改善で足りる。

## 6. 付録: 指標ベースライン(2026-06-06)
| 指標 | 値 |
|---|---|
| Python 行数(legacy除く) | 約 8,868 |
| 最大ファイル | agent.py 1,343 / main.py 1,228 / export.py 1,001 / app.js 2,587 |
| API ルート数 | 34(同期54・非同期3) |
| `except Exception` / 広い except | 92 / 108 |
| logging 呼び出し | 7 |
| 型注釈(戻り値) | 232/274(約85%) |
| テスト | 166 関数 / 10 ファイル / 全グリーン(独自ランナー) |
| CI・lint・型・lock | いずれも**なし** |

> 改善が進んだら本表を更新し、進捗の定点観測に使う。

---

## 7. 参考実装: odysseus(同一スタックの先行例)

[pewdiepie-archdaemon/odysseus](https://github.com/pewdiepie-archdaemon/odysseus) は本プロジェクトと
**同一スタック**(FastAPI / Python / SQLite / ChromaDB / Ollama 系 / JS フロント)の先行 OSS。
**層の分け方**を手本にできる(機能の丸ごと追随はしない)。

**層マッピング(odysseus → 本リポジトリ)**

| odysseus | 役割 | 本リポジトリの対応 |
|---|---|---|
| `routes/*_routes.py` | 薄い HTTP ルート(ドメイン別) | `app/routes/*_routes.py`(← `main.py` を分割) |
| `services/<domain>/` | 業務ロジック | `app/services/*_service.py` |
| `src/llm_core` | LLM transport の集約 | `app/llm.py`(**Ollama のまま維持**) |
| `src/agent_loop` / `agent_tools` | エージェントループ / ツール | `app/agent/loop.py` / `tools.py`(フェーズ2で分割) |
| `src/search/` | 検索 / RAG | `app/rag.py` / `retrieval.py` / `embeddings.py`(**e5 のまま**) |
| `core/`(auth/database/…) | 基盤 | `app/config.py` / `db.py` / `auth.py` / `safety.py` |

**ローカル LLM 線の維持(借りるのは層構造のみ)**
- LLM: odysseus は httpx+OpenAI互換。本リポジトリは **`ollama` ライブラリのまま**。
- 埋め込み: odysseus は fastembed(ONNX)。本リポジトリは **`multilingual-e5-base` のまま**(日本語精度)。
- Chroma: odysseus は別サービス。本リポジトリは **プロセス内のまま**(別サービス化は C4 対策の将来選択肢)。

**正直な注意点**
- odysseus でも `chat_stream` は約600行と太い。**完璧に薄いルートは目標にしない**。本当に効くのは
  「LLM・エージェントループ・ツールなどの**エンジンを routes から分離**」すること。
- 本リポジトリの**逐次承認+差分プレビュー**(`agent.py`)は odysseus より安全。フェーズ2の分割でも
  この承認フローは**退行させない**(独立モジュール化のみ)。

## 8. 進捗ログ
- **2026-06-06 / フェーズ1 着手(縦1スライス)**: `indexes`・一括要約を `main.py` から
  `app/routes/index_routes.py`(薄いルート)+ `app/services/index_service.py`(業務ロジック)へ
  **挙動不変**で切り出し。共有の `app/sse.py` を新設し、`app/routes/__init__.py` の `routers` を
  `main.py` が include する受け口を用意。
  - 効果: `main.py` **1,228 → 1,018 行**(-210)。単体テスト **12 追加・全グリーン**(回帰なし)。
  - LLM / 埋め込み / Chroma は現状維持(ローカル線そのまま)。
  - 次スライス候補: `conversations` → `chat(api_generate)` → `agent(api_agent)`。
- **2026-06-06 / フェーズ0 整備(品質ゲート)**: CI・lint・型・依存再現性を追加。
  - `.github/workflows/ci.yml`: push/PR で **ruff check + `python tests/run_all.py`**(ブロッキング)、
    **mypy は非ブロッキング**(情報提供。現状21件を段階解消)。
  - `pyproject.toml`: Python 3.11 明示、ruff(`E4/E7/E9+F`、E702/E501 は当面許容)、mypy(緩め)を設定。
  - `requirements-test.txt`: CI 用の最小依存(torch/pymupdf を除外、無い環境ではテストが自動 skip)。
  - `.github/dependabot.yml`: pip / GitHub Actions を週次更新。
  - 既存の未使用 import 4件(F401)を除去。**ruff 緑・全テスト緑**を確認。
  - 残課題: ruff ルールの段階強化(I/W/E501)、mypy のブロッキング化、ロックファイル(uv/pip-tools)。
- **2026-06-06 / フェーズ1 第2スライス(conversations)**: 会話・メッセージの CRUD を
  `app/routes/conversation_routes.py` + `app/services/conversation_service.py` へ**挙動不変**で切り出し。
  - `DELETE /api/conversations/{cid}` は Code エージェント実行時状態(`_code_ctx` 等)の掃除を
    伴うため main.py に残置(agent スライスで移設予定)。生成/添付/エージェントも別ドメインとして残置。
  - 効果: `main.py` **1,018 → 919 行**。単体テスト **11 追加・全グリーン**(全12ファイル緑)。
  - 次スライス候補: `agent`/`code`(`_code_ctx` 状態の移設込み)→ `chat(api_generate)`。
- **2026-06-06 / フェーズ1 第3スライス(meta/fs/export/ocr)＋全体デバッグ**:
  設定・認証・モデル一覧(meta)、フォルダ閲覧(fs)、ファイル出力(export)、OCR API を
  `app/routes/{meta,fs,export,ocr}_routes.py` へ**挙動不変**で分離(既存モジュールへ委譲する薄い層)。
  - 効果: `main.py` **919 → 703 行**(着手前 1,228 から計 **-525 / -43%**)。未使用 import 12件除去。
  - 全体デバッグ: ruff 緑 / 全12テスト緑 / mypy 21件(非ブロッキング・据え置き) /
    **ASGI スモーク(TestClient)で 6ルーターの実応答を確認**(config・settings・models・fs/roots・
    indexes・conversations(作成/取得)・export(md生成)・ocr(400) すべて PASS)。
  - main.py 残置: chat(generate/attachments)・agent(api_agent/code/file/`_code_ctx`)・app直下(/、/healthz、uploads)。
  - 次: フェーズ2(`agent.py` 分割＋`_code_ctx` 移設)→ chat スライス。
