# Auto モード(画面操作エージェント) 設計書

## 0. 命名について

| 項目 | 名称 | 備考 |
|---|---|---|
| 機能名(社内呼称) | **Auto モード** / **Auto 操作** | Chat / Code に並ぶ第3モード |
| UI タブ表記 | **Auto** | 4文字、Chat / Code と統一感 |
| DB 上の kind 値 | `auto` | `conversations.kind` の新値 |
| 内部モジュール名 | `app/auto.py` | `agent.py` と並列 |
| 国際表記(将来) | **Auto Operator** | 説明文・ドキュメント用 |

> 当初仮称「Cowork」は廃止し、機能の本質(画面を見て自動操作)が一目で分かる `Auto` に統一する。

---

## 1. 目的・スコープ

### 目的
ローカル LLM が **画面を見て**、ユーザの依頼に沿って **ブラウザを自動操作** し、一連の作業を完了させる。

### Phase 1 スコープ(本設計書の対象)
- **専用 Chromium 1 セッション** を Playwright で起動・制御
- ツール: スクショ取得 / URL 移動 / クリック / 入力 / キー操作 / スクロール / 待機 / セッション終了
- 操作前の承認フロー(計画モード)と緊急停止
- ライブスクショ表示 UI、操作履歴の永続化

### Phase 1 で **やらない** こと
- デスクトップ全体の操作(マウス・キーボード直接制御 → Phase 2)
- 複数ブラウザ・複数タブの並列操作
- 操作マクロの録画・再生(Phase 3)
- スマホ・モバイル UI のエミュレーション

---

## 2. 要件

### 機能要件
| ID | 要件 |
|---|---|
| F1 | ユーザが「○○して」と依頼を入力すると、AI が自動でブラウザ操作を行い目的を達成する |
| F2 | 各操作の前に内容(クリック対象・入力テキスト等)を表示し、承認を求めることができる |
| F3 | 計画モード時は、まず操作計画を提示して承認を取り、その後実行する |
| F4 | ライブスクリーンショットを画面右に表示し、AI が今見ている状況を可視化する |
| F5 | 緊急停止ボタンでループとブラウザを即座に終了できる |
| F6 | 操作履歴(スクショ + ツール呼出し + 結果)を会話履歴として保存し、再表示できる |
| F7 | 既定の Vision モデル(`qwen2.5-vl:32b` 等)を画面理解に使う |

### 非機能要件
| ID | 要件 |
|---|---|
| N1 | LAN_ONLY と `CHAT_PASSWORD` 認証は既存ルールを継承する |
| N2 | 外部送信なし。Ollama 経由のローカル推論のみ |
| N3 | 操作対象は **専用 Chromium プロセスのみ**。ユーザの個人ブラウザ・デスクトップに干渉しない |
| N4 | 1 会話 = 1 ブラウザセッション。複数会話同時実行可、ただし会話単位で排他 |
| N5 | 1 タスクあたり最大 60 ステップ(`MAX_STEPS`)で安全打切り |

---

## 3. UI 設計

### 3.1 タブ追加(Code の右隣)

`app/static/index.html:59-68` の `mode-tabs` を拡張:

```html
<div class="mode-tabs" id="mode-tabs">
  <button class="mode-tab active" data-mode="chat" title="チャット">...</button>
  <button class="mode-tab" data-mode="code" title="コード">...</button>
  <!-- 新規追加 -->
  <button class="mode-tab" data-mode="auto" title="自動操作(Auto)">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
         stroke-linecap="round" stroke-linejoin="round">
      <rect x="2" y="3" width="20" height="14" rx="2"/>
      <line x1="8" y1="21" x2="16" y2="21"/>
      <line x1="12" y1="17" x2="12" y2="21"/>
    </svg>
    <span>Auto</span>
  </button>
</div>
```

アイコンは「モニタ」を採用(画面を見て操作することを表現)。

### 3.2 画面レイアウト

```
┌─────────────────────────────────────────────────────────────┐
│ [☰] [Chat][Code][Auto*]  会話タイトル                  [🌓] │ ← header
├─────────────────────────────────────────────────────────────┤
│  📁 URL: [https://...     ][起動][終了]  [■緊急停止]        │ ← auto-bar (新規)
│  □ 計画モード   □ 各操作で確認   モデル:[qwen2.5-vl:32b ▼] │
├──────────────────────────────┬──────────────────────────────┤
│                              │                              │
│   メッセージ表示エリア       │   ライブスクショ表示         │
│   (既存 #messages 流用)     │   (#auto-screen 新規)       │
│                              │                              │
│   - ユーザ依頼               │   ┌──────────────────────┐  │
│   - 計画(計画モード)        │   │                      │  │
│   - ツール呼出し記録         │   │  Chromium スクショ   │  │
│   - スクショ縮小+結果        │   │  (リアルタイム更新)  │  │
│   - 完了報告                 │   │                      │  │
│                              │   └──────────────────────┘  │
│                              │   状態: 待機 / 実行中 / 承認待 │
├──────────────────────────────┴──────────────────────────────┤
│ [📎] 依頼を入力(例: 楽天市場でXXを検索して価格をまとめて)  │
└─────────────────────────────────────────────────────────────┘
```

### 3.3 既存 CSS パターンの拡張

`app/static/css/style.css:331-333` に倣い、表示制御クラスを追加:

```css
.only-auto { display: none; }
body[data-mode="auto"] .only-chat { display: none !important; }
body[data-mode="auto"] .only-code { display: none !important; }
body[data-mode="auto"] .only-auto { display: flex; }
body[data-mode="auto"] .auto-bar.only-auto { display: flex; }

/* ライブスクショペイン */
body[data-mode="auto"] .messages { width: 60%; }
.auto-screen { width: 40%; border-left: 1px solid var(--line); ... }
```

### 3.4 緊急停止ボタン
- 赤背景 + 大きめサイズで常時 `auto-bar` の右端に固定
- クリックで `POST /api/auto/abort` を呼ぶ → サーバ側でブラウザプロセス終了 + 生成ループ打切り

---

## 4. アーキテクチャ

### 4.1 全体構成図

```
┌───────────────────────────────────────────────────────────────┐
│ ブラウザ (フロント・既存 app.js を拡張)                        │
│   - Auto タブ / auto-bar / live screenshot pane                │
│   - SSE 受信 → メッセージ + スクショ更新                       │
└─────────────────────────┬─────────────────────────────────────┘
                          │ HTTP / SSE
┌─────────────────────────▼─────────────────────────────────────┐
│ FastAPI (app/main.py に追加するルート)                        │
│   POST /api/conversations/{cid}/auto    依頼受付・SSE 返却     │
│   POST /api/auto/approve                操作承認               │
│   POST /api/auto/answer                 ask_user 回答          │
│   POST /api/auto/abort                  緊急停止               │
└─────────────────────────┬─────────────────────────────────────┘
                          │ run_stream
┌─────────────────────────▼─────────────────────────────────────┐
│ Auto エージェント (app/auto.py = 新規)                        │
│   - ツールスキーマ定義(screenshot, browser_*)                │
│   - 実行ループ(agent.py の構造を踏襲)                       │
│   - ToolResult(text, image_b64) を返せるよう拡張              │
└─────────────────────────┬─────────────────────────────────────┘
                          │ Playwright API
┌─────────────────────────▼─────────────────────────────────────┐
│ BrowserSession (app/auto_browser.py = 新規)                  │
│   - Chromium プロセス管理(会話ごとに1個)                    │
│   - スクショ取得 / クリック / 入力 / ナビゲート               │
│   - user-data-dir: data/auto_browser/<conv_id>/              │
└───────────────────────────────────────────────────────────────┘
                          │ chat(messages, images=[...])
┌─────────────────────────▼─────────────────────────────────────┐
│ Ollama (既存)                                                 │
│   Vision モデル: qwen2.5-vl:32b / gemma3:27b                  │
└───────────────────────────────────────────────────────────────┘
```

### 4.2 既存コードとの責務分担

| 責務 | 既存 | 新規 |
|---|---|---|
| ツール呼出しループ全体構造 | `agent.run_stream()` パターン | `auto.run_stream()` で踏襲 |
| 承認・ask_user の resolve | `agent.resolve / resolve_answer` | **共通化**して両者で使う |
| 画像入力(messages.images) | `main.py:399-402` のパターン | `auto.run_stream()` で使う |
| ブラウザ操作レイヤ | なし | `app/auto_browser.py` 新設 |
| ツール定義 | `agent.py` のスキーマ集 | `auto.py` で独自に定義 |
| プロジェクト指示書読込 | `agent.read_project_instructions` | 流用しない(対象外) |

---

## 5. DB スキーマ

### 5.1 変更
**`conversations.kind`** に新値 `"auto"` を追加するのみ。**マイグレーション不要**(TEXT 列のため)。

### 5.2 `conversations.settings` JSON の拡張

Auto モード会話で使うキーを追加(他モードでは無視):

```json
{
  "vision_model": "qwen2.5-vl:32b",
  "auto": {
    "start_url": "https://www.google.com",
    "allowed_domains": ["google.com", "example.co.jp"],
    "headless": false,
    "viewport": { "width": 1280, "height": 800 },
    "confirm_each_action": true,
    "plan_mode": true
  }
}
```

| キー | 既定 | 説明 |
|---|---|---|
| `start_url` | (空) | セッション開始時に開く URL |
| `allowed_domains` | `[]` | 空なら全許可、指定ありならホワイトリスト |
| `headless` | `false` | サーバ側でブラウザを非表示で動かすか |
| `viewport` | 1280x800 | ブラウザ表示サイズ(モデルの推論精度に影響) |
| `confirm_each_action` | `true` | 計画モードオフ時の操作前確認 |
| `plan_mode` | `true` | 計画提示→承認→実行の段取りを取るか |

### 5.3 メッセージ保存
- ユーザ発話: 既存 `messages` テーブルにそのまま
- AI の作業ステップ: `messages.sources` (JSON) に `agent.py` と同じ形式で保存
  - 各ステップは `{type, name, args, status, result, image_b64?, diff?}`
  - **画像はサムネイル(WebP 80% 圧縮、最大 800px)を base64 で埋め込む**(履歴サイズ抑制)

---

## 6. 新規ツール仕様

すべて Auto モード固有。`auto.py` で定義する。

### 6.1 ツール一覧

| # | name | 引数 | 戻り値 | 承認要否 |
|---|---|---|---|---|
| 1 | `screenshot` | (なし) | スクショ画像(base64) + ページ URL + タイトル | 不要 |
| 2 | `browser_open` | `url` | 開いたページの URL / タイトル | **要** |
| 3 | `browser_navigate` | `url` | 遷移後の URL / タイトル | **要** |
| 4 | `browser_click` | `selector?`, `x?`, `y?`, `description` | クリック結果 + 新スクショ | **要** |
| 5 | `browser_type` | `selector?`, `text`, `submit?` | 入力結果 + 新スクショ | **要** |
| 6 | `browser_press_key` | `key`(例: `"Enter"`, `"Control+a"`) | 結果 + 新スクショ | **要** |
| 7 | `browser_scroll` | `direction`(up/down), `amount?` | 新スクショ | 不要 |
| 8 | `browser_wait` | `seconds?`, `selector?` | 待機完了 / タイムアウト | 不要 |
| 9 | `browser_read_text` | `selector?` | 要素テキスト(本文抽出用) | 不要 |
| 10 | `browser_close` | (なし) | セッション終了 | 不要 |
| 11 | `ask_user` | `question`, `options[]` | ユーザ回答(既存共通化) | 待機 |
| 12 | `present_plan` | `plan`(Markdown) | 承認結果(既存共通化) | 待機 |
| 13 | `todo_write` | `todos[]` | 確認のみ | 不要 |

### 6.2 ツールスキーマ例(2件のみ抜粋)

```python
_T_SHOT = {"type": "function", "function": {
    "name": "screenshot",
    "description": "現在のブラウザ画面のスクリーンショットを取得する。次の判断材料として使う。",
    "parameters": {"type": "object", "properties": {}, "required": []}}}

_T_CLICK = {"type": "function", "function": {
    "name": "browser_click",
    "description": "ブラウザ上の要素をクリックする。selector(CSS)を優先、難しい場合のみ座標を使う。",
    "parameters": {"type": "object", "properties": {
        "selector": {"type": "string", "description": "CSS セレクタ(例: 'button#login')。優先"},
        "x": {"type": "integer", "description": "座標X(selectorが使えない場合のみ)"},
        "y": {"type": "integer", "description": "座標Y(selectorが使えない場合のみ)"},
        "description": {"type": "string", "description": "何をクリックするかの説明(承認画面に表示)"}
    }, "required": ["description"]}}}
```

### 6.3 ツール実行後の挙動
- すべての変更系ツール(`browser_open`, `*_navigate`, `*_click`, `*_type`, `*_press_key`)は
  **実行後に screenshot を自動的に取得し、tool_result に画像として添える**
  → これにより LLM が「次のターンで自然に最新画面を見る」流れになる
- これがエージェント精度の鍵。明示的に screenshot を呼ぶ必要性を減らす

---

## 7. 画像伝播フロー(コア設計)

### 7.1 課題
既存 `app/agent.py` の `tool_result` イベントは **文字列(text)のみ**。
Auto モードでは「ツール結果 = スクショ画像」を次の LLM ターンに渡す必要がある。

### 7.2 解決方針

#### (a) `auto.py` の内部データ構造拡張
```python
class ToolResult:
    text: str
    image_b64: Optional[str] = None        # スクショ
    image_mime: str = "image/png"
    status: str = "ok"                     # ok / error / denied / timeout
```

#### (b) Ollama への次ターン投入(`auto.run_stream()` 内)
```python
# tool_result を受け取った直後、次の LLM 呼出しに画像を載せる
messages.append({"role": "tool", "name": tname, "content": tr.text})
if tr.image_b64:
    # Ollama は最後の user/system ターンの images に画像を載せる仕様
    # → tool 直後の "user" 役で「最新スクショです」と画像を渡す擬似ターンを挿入
    messages.append({
        "role": "user",
        "content": "(最新のスクリーンショット)",
        "images": [tr.image_b64],
    })
```

> Ollama Python SDK は `role: tool` メッセージに `images` を直接持たせる仕様が安定していないため、
> **「user 役の擬似ターン」を挟む** のが現実的。`main.py:399-402` の既存パターンと同じ。

#### (c) DB 保存時はサムネイル化
- フル解像度(1280×800 PNG ~1MB)をすべて DB に保存するとサイズ爆発
- 履歴保存時は **WebP 圧縮で最大幅 800px、~50KB に縮小** して `messages.sources` に格納
- ライブ表示にはフル解像度をストリーミング(保存しない)

### 7.3 イベントスキーマ拡張

`auto.run_stream()` が SSE で吐くイベント:

```python
{"type": "tool_call",   "name": "browser_click", "args": {...}}
{"type": "tool_result", "name": "browser_click", "status": "ok",
 "result": "要素 #login をクリックしました",
 "image_b64": "<縮小スクショ>",        # ★ 新規
 "image_full_url": "/api/auto/...png"}  # ★ ライブ表示用(別エンドポイント)
{"type": "screenshot",  "image_b64": "..."}  # ★ 画面のみ更新時(明示の screenshot 呼出し)
{"type": "approval_required", "action_id": "...", "summary": "..."}
{"type": "ask", "action_id": "...", "question": "...", "options": [...]}
{"type": "plan", "plan": "..."}
{"type": "todos", "todos": [...]}
{"type": "done", "message": {...}}
{"type": "error", "error": "..."}
```

---

## 8. API 設計

### 8.1 新規エンドポイント

```python
# app/main.py に追加
@app.post("/api/conversations/{cid}/auto",
          dependencies=[Depends(auth.require_auth)])
def api_auto(cid: str, body: AgentBody) -> Response:
    """Auto モード実行。SSE で進捗ストリーム。"""

@app.post("/api/auto/approve", dependencies=[Depends(auth.require_auth)])
def api_auto_approve(body: ApproveBody) -> dict:
    """操作承認(Code の approve と同形式)。共通化を検討。"""

@app.post("/api/auto/answer", dependencies=[Depends(auth.require_auth)])
def api_auto_answer(body: AnswerBody) -> dict:
    """ask_user への回答。"""

@app.post("/api/auto/abort", dependencies=[Depends(auth.require_auth)])
def api_auto_abort(cid: str = Body(..., embed=True)) -> dict:
    """緊急停止: ループ中断 + ブラウザ即終了。"""

@app.get("/api/auto/{cid}/live.png",
         dependencies=[Depends(auth.require_auth)])
def api_auto_live(cid: str) -> Response:
    """ライブスクショ(現在の画面)。ポーリング用。
       Phase 1.5 で WebSocket / SSE プッシュへ移行検討。"""
```

### 8.2 状態管理

`main.py` 既存の Code エージェントに倣い、メモリ保持:

```python
_auto_ctx: dict[str, list] = {}                # 会話ごとの会話履歴(messages)
_auto_sessions: dict[str, BrowserSession] = {} # 会話ごとのブラウザ
_auto_running: set[str] = set()                # 実行中の会話 ID
_auto_lock = threading.Lock()
```

### 8.3 会話削除時のクリーンアップ
`api_delete_conversation` で Code と同様の処理を追加:
```python
with _auto_lock:
    sess = _auto_sessions.pop(cid, None)
    if sess: sess.close()
    _auto_ctx.pop(cid, None)
    _auto_running.discard(cid)
```

---

## 9. セキュリティ設計

### 9.1 操作スコープ(技術的サンドボックス)
- **専用 Chromium プロセス** を Playwright で起動。ユーザの個人ブラウザと分離
- `user-data-dir = data/auto_browser/<conv_id>/` に固定 → クッキー・ログイン情報も会話内で完結
- Phase 1 では **デスクトップに触れない** → 暴走しても被害はブラウザ内のみ

### 9.2 承認フロー
| モード | 動作 |
|---|---|
| 計画モード ON(既定) | `present_plan` で承認 → 以降の操作はクリック・入力等を**サマリ付きで承認** |
| 計画モード OFF + 各操作で確認 ON | すべての変更系操作を **個別に承認** |
| 計画モード OFF + 各操作で確認 OFF | 操作前承認なし。自動実行モード(注意喚起トースト必須) |

### 9.3 URL ホワイトリスト
- `settings.auto.allowed_domains` が空でない場合、`browser_open` / `browser_navigate` 実行前に
  ドメイン照合(`fnmatch` で `*.example.com` 形式対応)
- マッチしなければツール側で deny し、エラー結果を LLM に返す

### 9.4 緊急停止
- フロント: 赤い「■ 停止」ボタン → `POST /api/auto/abort`
- サーバ:
  1. `_auto_running` から除外
  2. SSE ストリームの `GeneratorExit` 発火(クライアント切断と同等)
  3. `BrowserSession.close()` を強制実行(Playwright `browser.close()`)
- LLM 推論中の Ollama リクエストは Ollama 側のキャンセル機構に依存(タイムアウト)

### 9.5 認証・LAN 制限
- 既存 `auth.require_auth` をそのまま適用
- `LAN_ONLY` ミドルウェアも自動的に適用される(`main.py:82-93`)
- README に「Auto モードはサーバ上でブラウザを起動するため、Code と同様に **必ず `CHAT_PASSWORD` 設定**」を追記

### 9.6 ログ
- 操作前後のスクショ + ツール呼出し引数を構造化ログに出力
- 監査用に `data/auto_audit/<conv_id>/<timestamp>.json` へ追記(オプション・既定 OFF)

---

## 10. ファイル単位の改修一覧

### 10.1 新規ファイル

| ファイル | 内容 | 行数目安 |
|---|---|---|
| `app/auto.py` | エージェントループ・ツールスキーマ・承認解決 | ~500 |
| `app/auto_browser.py` | Playwright ラッパ。BrowserSession クラス | ~300 |
| `docs/auto_design.md` | 本設計書 | (本ファイル) |
| `docs/auto_user_guide.md` | エンドユーザ向け使い方(Phase 1 完了時) | ~150 |

### 10.2 修正ファイル

| ファイル | 修正点 |
|---|---|
| `app/main.py` | `/api/conversations/{cid}/auto` 等 5 ルート追加、`_auto_*` 状態、会話削除時クリーンアップ |
| `app/db.py` | `kind="auto"` を許容(現状でも動くが定数化推奨)。`messages.sources` の解釈はそのまま流用 |
| `app/agent.py` | 承認 resolve 関数(`resolve`, `resolve_answer`)を `auto.py` から再利用するため公開 API に格上げ。または共通モジュール `app/agent_common.py` を切り出す |
| `app/safety.py` | `is_url_allowed(url, allowed_domains)` 関数追加 |
| `app/config.py` | `AUTO_BROWSER_HEADLESS`, `AUTO_BROWSER_VIEWPORT` 等の環境変数読込追加 |
| `app/static/index.html` | mode-tabs に Auto 追加、`#auto-bar`, `#auto-screen` ペイン追加 |
| `app/static/css/style.css` | `.only-auto`, `body[data-mode="auto"]`, `.auto-bar`, `.auto-screen` 等 |
| `app/static/js/app.js` | `setMode("auto")` 対応、Auto バー操作、ライブスクショ更新、緊急停止 |
| `requirements.txt` | `playwright>=1.40`, `Pillow>=10`(スクショ圧縮用) |
| `.env.example` | `AUTO_BROWSER_HEADLESS=false` 等の追加 |
| `README.md` | 機能紹介セクション、起動手順(`playwright install chromium`)、安全注意 |

---

## 11. 段階計画

### Phase 1(本設計の中核 / 推定 5〜7 営業日)
- [ ] Playwright 統合 (`auto_browser.py`)
- [ ] ツール 10 個実装(`screenshot`, `browser_*`, `ask_user`, `present_plan`, `todo_write`)
- [ ] 画像伝播(ToolResult, 擬似 user ターン)
- [ ] `auto.py` のエージェントループ
- [ ] API ルート + 状態管理
- [ ] UI: Auto タブ、auto-bar、ライブスクショ(ポーリング)、緊急停止
- [ ] 承認フロー UI(差分の代わりに操作サマリ表示)
- [ ] DB 保存(サムネイル化)
- [ ] 動作確認シナリオ(§13)を通す

### Phase 1.5(精度向上 / 推定 2〜3 営業日)
- [ ] ライブスクショを SSE プッシュへ
- [ ] URL ホワイトリスト UI
- [ ] 計画モード時の TODO 進捗表示
- [ ] 失敗時の自動リトライ(同じ操作を 2 回まで)

### Phase 2(デスクトップ拡張 / 推定 3〜5 営業日)
- [ ] PyAutoGUI 統合(別ツール群 `desktop_*`)
- [ ] OS 別の緊急停止ホットキー
- [ ] アプリ起動ツール

### Phase 3(高度化 / 推定 5〜10 営業日)
- [ ] 操作録画 → スクリプト保存
- [ ] 録画したスクリプトをパラメータ化して再実行
- [ ] よく使うサイト用の知識ベース連携(RAG と統合)

---

## 12. リスクと緩和策

| # | リスク | 影響 | 緩和策 |
|---|---|---|---|
| R1 | ローカル LLM のツール呼出し精度 | 中 | Vision モデルは `qwen2.5-vl:32b` を既定推奨、設定でいつでも切替可。`gemma3:27b` 等もフォールバック |
| R2 | 座標クリックの解像度依存ずれ | 中 | セレクタ操作を最優先、座標は最終手段。ビューポート固定(1280×800) |
| R3 | LLM 無限ループ | 中 | `MAX_STEPS=60`、直近 N ステップで同操作の繰り返しを検出して打切り |
| R4 | VRAM 競合(Chat + Vision + Embedding 同時常駐) | 中 | Ollama `keep_alive=5m` を `auto` 用ターン中だけ 0 にして他モデル解放。設定で切替 |
| R5 | Playwright 初回 install | 低 | README に `playwright install chromium` を明記。`run.py` 起動時に未インストール検知警告 |
| R6 | ヘッドレス vs 通常 | 中 | サーバが Linux server で GUI なしの場合 `headless=true` 必須。Windows デスクトップなら好みで選択 |
| R7 | 機微情報のスクショ保存 | 高 | 履歴サムネイルは既定 ON、設定で OFF 可。ログイン情報は `user-data-dir` 内に閉じる |
| R8 | 同時アクセス時のセッション衝突 | 低 | 会話単位で `_auto_running` で排他、別会話なら並列可 |
| R9 | LAN 公開時に他人が誤って操作 | 高 | `CHAT_PASSWORD` 必須化(README で強調)、Auto モードは認証ユーザのみ |
| R10 | サイト側の自動化対策(reCAPTCHA 等) | 中 | 対策不可と明記、ask_user で人間に介入を求める動線 |

---

## 13. 動作確認シナリオ(Phase 1 受入テスト)

| # | シナリオ | 期待動作 |
|---|---|---|
| T1 | 「example.com にアクセスしてタイトルを教えて」 | `browser_open` → `screenshot` → タイトル抽出 → 完了報告 |
| T2 | 「Google で『RAG とは』を検索して、トップ 3 件のタイトルをまとめて」 | open → type → press_key(Enter) → スクショ理解 → 整理出力 |
| T3 | 計画モード ON で T2 を実行 | `present_plan` で承認待ち → 承認後に操作開始 |
| T4 | 計画承認後、`browser_click` でも逐一承認を求めるよう設定 | 各操作前に承認ダイアログ |
| T5 | 緊急停止ボタンを押す | ループ即停止、ブラウザ即終了、エラーメッセージ表示 |
| T6 | `allowed_domains=["example.com"]` で google.com を開かせる | ツール側で deny、エラー結果が LLM に返り別案 |
| T7 | 会話削除 | ブラウザプロセス終了、`data/auto_browser/<cid>/` クリーンアップ |
| T8 | 再起動して同会話を開く | 過去のスクショ・操作履歴が DB から復元表示される |
| T9 | LLM が同じクリックを 3 回繰り返す | ループ検出で打切り、ユーザにエラー報告 |
| T10 | ask_user 発火 → ユーザ回答 → 続行 | 期待通り回答が反映される |

---

## 14. 未決事項・要相談

| # | 項目 | 候補 |
|---|---|---|
| Q1 | アイコン | モニタ風 / ロボット風 / 🌐 のいずれを採用するか |
| Q2 | ライブスクショ更新間隔 | 操作完了ごと(イベント駆動) / 500ms ポーリング / WebSocket |
| Q3 | デスクトップ操作の Phase 2 着手判断 | Phase 1 の精度評価後に判断 |
| Q4 | 録画機能の方式(Phase 3) | Playwright トレース機能を利用 / 独自記録 |
| Q5 | RAG 連携 | 「特定サイトの操作手順書を RAG で参照しながら Auto モードが動く」設計を将来盛り込むか |
| Q6 | macOS / Linux サポート | Phase 1 は Windows + ヘッドレス Linux 動作確認。macOS は best effort |

---

## 15. 変更による既存機能への影響

| 機能 | 影響 |
|---|---|
| Chat モード | なし |
| Code モード | `agent.resolve / resolve_answer` を共通化する場合、軽微なリファクタ発生 |
| 参照資料(RAG) | なし(Phase 3 で連携検討) |
| 認証・LAN_ONLY | なし(継承) |
| 既存 DB スキーマ | なし(`kind` の値を増やすのみ) |

---

## 16. 参考: 既存資産で「そのまま使えるもの」

| 既存実装 | 流用先 |
|---|---|
| `agent.run_stream()` のループ構造 | `auto.run_stream()` |
| `agent.resolve()` / `resolve_answer()` | 承認・ask_user 解決 |
| `agent.compact_ctx_with_model()` | 文脈圧縮(長期作業時) |
| `main.py:399-402` の images 投入パターン | スクショ画像の messages 注入 |
| `main.py:_code_ctx` の状態管理パターン | `_auto_ctx` / `_auto_sessions` |
| `db.add_message` / `list_messages` | メッセージ永続化 |
| `messages.sources` の構造化ステップ保存 | スクショ + ツール記録 |
| SSE ヘルパ `sse()` | 進捗ストリーム |
| 既存 `mode-tab` / `data-mode` の CSS パターン | Auto タブ |
| `auth.require_auth` 依存 | 全 API |

---

**設計書 v0.1 / 着手前レビュー版**
