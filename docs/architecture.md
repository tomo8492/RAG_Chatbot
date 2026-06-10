# アーキテクチャ / 並行性モデル

RAG_Chatbot の構成と、**プロセス内共有状態・並行性の前提**をまとめる。
リファクタ(コード改善ロードマップ フェーズ1/2)の到達点でもある。

- 関連: [code-roadmap.md](code-roadmap.md)(技術的負債・進捗)
- 最終更新: 2026-06-06

---

## 1. レイヤ構成

```
app/
  main.py            FastAPI 本体。lifespan・LANガード・残りのルート(chat/agent/code/static)
  routes/            HTTP ルート層(薄い: 検証→service/engine 呼び出し→整形)
    meta / conversation / index / fs / export / ocr _routes.py
  services/          業務ロジック(HTTP非依存)
    conversation_service / index_service
  agent/             コーディングエージェント(パッケージ。下記 §3)
    constants / tools / approvals / context / _impl(engine) / __init__(facade)
  rag / retrieval / embeddings / splitter / loaders / summarize / export / ocr / postprocess
  config / db / defaults / auth / safety / sse / logging_setup   基盤
```

呼び出しの向き(上→下、循環なし):
`routes → services → (rag / summarize / export / agent / db / llm)`。
`main.py` は `routes/` の router を include し、自身は chat/agent/code/static のみ保持。

---

## 2. 並行性モデル(重要)

### 2.1 大前提: 単一プロセス(`uvicorn --workers=1`)
本アプリは **1プロセス・マルチスレッド**で動く前提。下表の状態は**プロセス内メモリ**に
あり、ワーカ間で共有されない。**`--workers>1` で起動すると、承認待ち・実行中フラグ・
バックグラウンドジョブ・コード作業コンテキストがワーカ間でちぐはぐになり破綻する。**
複数ワーカ/多ノードへ広げる場合は、これらの揮発状態を DB / Redis / キューへ出す必要がある(将来課題)。

FastAPI の同期 `def` ルートはスレッドプールで実行される。共有状態は**ロックで保護**する。

### 2.2 共有可変状態の一覧

| 状態 | 場所 | ロック | 用途 / 寿命 |
|---|---|---|---|
| `_pending` | `agent/approvals.py` | `_pending_lock` | 承認/回答待ち(action_id→Event)。解決で pop |
| `_bg_jobs` | `agent/tools.py` | `_bg_lock` | バックグラウンド実行コマンド(job_id→proc)。最大 `MAX_BG_JOBS` |
| `_UNDO` | `agent/_impl.py` | (なし) | 直近変更の undo 情報(undo_id→旧内容)。キーは一意・低競合のため許容 |
| `_code_ctx` / `_code_running` | `main.py` | `_code_ctx_lock` | 会話ごとのコードagent文脈 / 実行中フラグ |
| `_summary_cancel` | `services/index_service.py` | `_summary_lock` | 一括要約の中止フラグ(裏実行) |
| `_CAPS_CACHE` | `llm.py` | (なし) | モデル能力の読み取り中心キャッシュ(冪等のため許容) |
| 埋め込みシングルトン | `embeddings.py` | `_lock`/`_default_lock` | 埋め込みモデルの遅延生成 |
| Chroma クライアント | `rag.py` | `_client_lock` | クライアントの遅延生成 |
| SQLite 書込み | `db.py` | `_write_lock` | 書込みを直列化(WAL・呼び出し毎接続) |

### 2.3 バックグラウンドスレッド(daemon)
- **索引構築**: `index_service.build_async` → `rag.build_index`
- **一括要約(裏)**: `index_service._summarize_worker`(`_summary_cancel` で中止)
- **コマンド実行の出力読取**: `agent/tools.py _bg_reader`

いずれも daemon スレッド。進捗/結果は DB(kv)やジョブ辞書経由で参照する。

### 2.4 ロックを置いていない状態の理由
- `_UNDO`: キーが一意の undo_id で、書き手は当該リクエストのみ。実害のある競合がない。
- `_CAPS_CACHE`: 読み取り中心・値は冪等(同じモデルなら同じ結果)。最悪でも二重計算のみ。

> 将来 `--workers>1` 化や厳密性が必要になれば、`_UNDO`/`_CAPS_CACHE` のロック化、
> および §2.1 の揮発状態の外部ストア化を検討する。

---

## 3. agent パッケージ(フェーズ2の分割)

神モジュールだった `agent.py`(1,343行)をパッケージへ分割。公開APIは
`__init__.py`(ファサード)で温存し、`app.agent.X` の参照は不変。

| モジュール | 役割 | 依存 |
|---|---|---|
| `constants.py` | 定数・ツールスキーマ・システムプロンプト(leafデータ) | (なし) |
| `tools.py` | ツール(`t_*`)・`dispatch`・バックグラウンドジョブ | constants, safety |
| `approvals.py` | 承認/回答ステートマシン(`_pending`) | constants |
| `context.py` | 文脈圧縮(純関数。要約関数は注入) | constants |
| `_impl.py` | **エンジン**: 変更プレビュー/適用/undo・自己検証・生成ループ(`run_stream`) | 上記すべて + llm |
| `__init__.py` | ファサード(公開API再エクスポート) | — |

依存は一方向(`_impl → {tools, approvals, context, constants}`)。

> 補足: プレビュー/適用/undo と `run_stream` は相互に密結合(承認フローと一体)のため、
> 分割しすぎず `_impl`(エンジン)に集約している。さらなる分割より**結合の明確化**を優先。

---

## 4. リクエストの流れ(例: チャット生成)
```
ブラウザ → POST /api/conversations/{cid}/generate (main.py)
  → 認証(auth.require_auth) / LANガード(_lan_guard)
  → 設定マージ(defaults.effective_for) / RAG 検索(rag.retrieve)
  → llm ストリーム生成 → SSE(sse.sse)で逐次返却 → DB へ保存(db)
```
コードエージェントは `/api/conversations/{cid}/agent` → `agent.run_stream`、
承認は `/api/code/approve|answer` → `agent.resolve|resolve_answer`(`_pending` 経由)。
