# Automa モード(画面操作エージェント) 設計書

## 0. 命名について

| 項目 | 名称 | 備考 |
|---|---|---|
| 機能名(社内呼称) | **Automa モード** / **Automa 操作** | Chat / Code に並ぶ第3モード |
| UI タブ表記 | **Automa** | 4文字、Chat / Code と統一感 |
| DB 上の kind 値 | `automa` | `conversations.kind` の新値 |
| 内部モジュール名 | `app/automa.py` | `agent.py` と並列 |
| 国際表記(将来) | **Automa Operator** | 説明文・ドキュメント用 |

> 当初仮称「Cowork」は廃止し、機能の本質(画面を見て自動操作)が一目で分かる `Automa` に統一する。

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
  <button class="mode-tab" data-mode="automa" title="自動操作(Automa)">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
         stroke-linecap="round" stroke-linejoin="round">
      <rect x="2" y="3" width="20" height="14" rx="2"/>
      <line x1="8" y1="21" x2="16" y2="21"/>
      <line x1="12" y1="17" x2="12" y2="21"/>
    </svg>
    <span>Automa</span>
  </button>
</div>
```

アイコンは「モニタ」を採用(画面を見て操作することを表現)。

### 3.2 画面レイアウト

```
┌─────────────────────────────────────────────────────────────┐
│ [☰] [Chat][Code][Automa*]  会話タイトル                  [🌓] │ ← header
├─────────────────────────────────────────────────────────────┤
│  📁 URL: [https://...     ][起動][終了]  [■緊急停止]        │ ← automa-bar (新規)
│  □ 計画モード   □ 各操作で確認   モデル:[qwen2.5-vl:32b ▼] │
├──────────────────────────────┬──────────────────────────────┤
│                              │                              │
│   メッセージ表示エリア       │   ライブスクショ表示         │
│   (既存 #messages 流用)     │   (#automa-screen 新規)       │
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
.only-automa { display: none; }
body[data-mode="automa"] .only-chat { display: none !important; }
body[data-mode="automa"] .only-code { display: none !important; }
body[data-mode="automa"] .only-automa { display: flex; }
body[data-mode="automa"] .automa-bar.only-automa { display: flex; }

/* ライブスクショペイン */
body[data-mode="automa"] .messages { width: 60%; }
.automa-screen { width: 40%; border-left: 1px solid var(--line); ... }
```

### 3.4 緊急停止ボタン
- 赤背景 + 大きめサイズで常時 `automa-bar` の右端に固定
- クリックで `POST /api/automa/abort` を呼ぶ → サーバ側でブラウザプロセス終了 + 生成ループ打切り

---

## 4. アーキテクチャ

### 4.1 全体構成図

```
┌───────────────────────────────────────────────────────────────┐
│ ブラウザ (フロント・既存 app.js を拡張)                        │
│   - Automa タブ / automa-bar / live screenshot pane                │
│   - SSE 受信 → メッセージ + スクショ更新                       │
└─────────────────────────┬─────────────────────────────────────┘
                          │ HTTP / SSE
┌─────────────────────────▼─────────────────────────────────────┐
│ FastAPI (app/main.py に追加するルート)                        │
│   POST /api/conversations/{cid}/automa    依頼受付・SSE 返却     │
│   POST /api/automa/approve                操作承認               │
│   POST /api/automa/answer                 ask_user 回答          │
│   POST /api/automa/abort                  緊急停止               │
└─────────────────────────┬─────────────────────────────────────┘
                          │ run_stream
┌─────────────────────────▼─────────────────────────────────────┐
│ Automa エージェント (app/automa.py = 新規)                        │
│   - ツールスキーマ定義(screenshot, browser_*)                │
│   - 実行ループ(agent.py の構造を踏襲)                       │
│   - ToolResult(text, image_b64) を返せるよう拡張              │
└─────────────────────────┬─────────────────────────────────────┘
                          │ Playwright API
┌─────────────────────────▼─────────────────────────────────────┐
│ BrowserSession (app/automa_browser.py = 新規)                  │
│   - Chromium プロセス管理(会話ごとに1個)                    │
│   - スクショ取得 / クリック / 入力 / ナビゲート               │
│   - user-data-dir: data/automa_browser/<conv_id>/              │
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
| ツール呼出しループ全体構造 | `agent.run_stream()` パターン | `automa.run_stream()` で踏襲 |
| 承認・ask_user の resolve | `agent.resolve / resolve_answer` | **共通化**して両者で使う |
| 画像入力(messages.images) | `main.py:399-402` のパターン | `automa.run_stream()` で使う |
| ブラウザ操作レイヤ | なし | `app/automa_browser.py` 新設 |
| ツール定義 | `agent.py` のスキーマ集 | `automa.py` で独自に定義 |
| プロジェクト指示書読込 | `agent.read_project_instructions` | 流用しない(対象外) |

---

## 5. DB スキーマ

### 5.1 変更
**`conversations.kind`** に新値 `"automa"` を追加するのみ。**マイグレーション不要**(TEXT 列のため)。

### 5.2 `conversations.settings` JSON の拡張

Automa モード会話で使うキーを追加(他モードでは無視):

```json
{
  "vision_model": "qwen2.5-vl:32b",
  "automa": {
    "start_url": "https://www.google.com",
    "allowed_domains": ["google.com", "example.co.jp"],
    "headless": false,
    "viewport": { "width": 1280, "height": 800 },
    "confirm_each_action": true,
    "plan_mode": true,
    "ephemeral": false,
    "save_screenshots": true
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
| `ephemeral` | `false` | **true で Cookie/ログイン状態を保存しない**(銀行系等の機微サイト用)。`user-data-dir` を tmp に切替、セッション終了時に削除 |
| `save_screenshots` | `true` | スクショサムネイルを履歴 DB に残すか。機微画面を残したくない場合は OFF |

### 5.3 メッセージ保存
- ユーザ発話: 既存 `messages` テーブルにそのまま
- AI の作業ステップ: `messages.sources` (JSON) に `agent.py` と同じ形式で保存
  - 各ステップは `{type, name, args, status, result, image_b64?, diff?}`
  - **画像はサムネイル(WebP 80% 圧縮、最大 800px)を base64 で埋め込む**(履歴サイズ抑制)

---

## 6. 新規ツール仕様

すべて Automa モード固有。`automa.py` で定義する。

### 6.1 要素特定方式: Set-of-Marks の採用(設計判断)

ローカル VLM(qwen2.5-vl:32b / gemma3:27b 等)の座標推定は本家 Claude Computer Use と比べ
**数十 px のずれを起こしやすい**。これを設計レベルで回避するため、**Set-of-Marks (SoM)** を採用する。

#### 3つのパラダイム比較

| 方式 | 仕組み | 強み | 弱み |
|---|---|---|---|
| ピクセル/座標(Computer Use 型) | スクショ → モデルが座標を推定 | canvas/非DOM も操作可 | ローカル VLM の座標推定が不安定 |
| DOM/セレクタ(Playwright 純正) | DOM ツリーをモデルに見せ CSS セレクタで指定 | 確実・決定的 | DOM 抽出が要・長いツリーがコンテキストを食う |
| **Set-of-Marks(採用)** | 操作可能要素に**番号付き枠をスクショに重畳**、モデルは「7 番」と指定 | 視覚同定 + 確実な解決の両取り、推論が単純化 | スクショ前処理が要 |

#### SoM の動作

`screenshot()` ツールは以下のペアを返す:

1. **画像**: 各クリック可能要素に番号付き赤枠を Playwright + Pillow で重畳した PNG
2. **要素リスト**: `[{mark_id: 1, tag: "button", text: "ログイン", role: "button"}, ...]`(JSON)

モデルは `browser_click(mark_id=7)` のように番号で指定。
サーバ側で `mark_id → Playwright Locator` に解決して実クリック。

#### 採用理由
- **ローカル VLM の弱い座標推定を完全に回避**(番号判定だけで足りる)
- DOM 全文を見せずに済む(コンテキスト節約 → §7.4 と整合)
- WebVoyager / SeeAct 等の Web 自動化研究で実績ある方式
- 「番号付け不能な要素」(canvas 内 / shadow DOM 等)のみ、フォールバックで座標 or CSS セレクタを使う

#### 引数優先順位
**`mark_id` > `selector` > `x, y`**。`mark_id` が指定されていればそれだけで解決し、他は無視。

### 6.2 ツール一覧

| # | name | 引数 | 戻り値 | 承認要否 |
|---|---|---|---|---|
| 1 | `screenshot` | (なし) | **SoM 重畳画像 + 要素リスト** + URL + タイトル | 不要 |
| 2 | `browser_open` | `url` | 開いたページの URL / タイトル + 新 SoM スクショ | **要** |
| 3 | `browser_navigate` | `url` | 遷移後の URL / タイトル + 新 SoM スクショ | **要** |
| 4 | `browser_click` | **`mark_id?`**, `selector?`, `x?`, `y?`, `description` | クリック結果 + 新 SoM スクショ | **要** |
| 5 | `browser_type` | **`mark_id?`**, `selector?`, `text`, `submit?` | 入力結果 + 新 SoM スクショ | **要** |
| 6 | `browser_press_key` | `key`(例: `"Enter"`, `"Control+a"`) | 結果 + 新 SoM スクショ | **要** |
| 7 | `browser_scroll` | `direction`(up/down), `amount?` | 新 SoM スクショ | 不要 |
| 8 | `browser_wait` | `seconds?`, `selector?`, **`mark_id?`** | 待機完了 / タイムアウト | 不要 |
| 9 | `browser_read_text` | **`mark_id?`**, `selector?` | 要素テキスト(本文抽出用) | 不要 |
| 10 | `browser_close` | (なし) | セッション終了 | 不要 |
| 11 | `ask_user` | `question`, `options[]` | ユーザ回答(共通化) | 待機 |
| 12 | `present_plan` | `plan`(Markdown) | 承認結果(共通化) | 待機 |
| 13 | `todo_write` | `todos[]` | 確認のみ | 不要 |

### 6.3 ツールスキーマ例(2件のみ抜粋)

```python
_T_SHOT = {"type": "function", "function": {
    "name": "screenshot",
    "description": "現在のブラウザ画面を撮影する。操作可能な要素には番号付き枠が描かれ、"
                   "要素リスト(mark_id, tag, text, role)が同時に返る。"
                   "クリック・入力の対象は原則 mark_id で指定する。",
    "parameters": {"type": "object", "properties": {}, "required": []}}}

_T_CLICK = {"type": "function", "function": {
    "name": "browser_click",
    "description": "ブラウザ上の要素をクリックする。screenshot で得た mark_id を最優先で使う。"
                   "番号が付かない要素のみ selector または x,y を使う。",
    "parameters": {"type": "object", "properties": {
        "mark_id": {"type": "integer",
                    "description": "screenshot 結果の要素番号(最優先)"},
        "selector": {"type": "string",
                     "description": "CSS セレクタ(mark_id が使えない場合)"},
        "x": {"type": "integer",
              "description": "座標X(mark_id/selector が共に使えない場合のみ)"},
        "y": {"type": "integer", "description": "座標Y"},
        "description": {"type": "string",
                        "description": "何をクリックするかの説明(承認画面に表示)"}
    }, "required": ["description"]}}}
```

### 6.4 ツール実行後の挙動
- すべての変更系ツール(`browser_open`, `*_navigate`, `*_click`, `*_type`, `*_press_key`, `*_scroll`)は
  **実行後に SoM スクショを自動的に取得し、tool_result に画像 + 要素リストとして添える**
  → LLM が「次のターンで自然に最新画面を見る」流れになる
- これがエージェント精度の鍵。明示的に `screenshot` を呼ぶ必要性を減らす

### 6.5 SoM 抽出の実装メモ
- Playwright の `page.locator(...).all()` + アクセシビリティツリー(`page.accessibility.snapshot()`)から候補抽出
- 抽出対象: `button`, `a`, `input`, `select`, `textarea`, `[role="button"]`, `[role="link"]`, `[onclick]`, `[tabindex]>=0`
- 各要素の `bounding_box` を取得 → Pillow で番号 + 枠を画像に描画
- 最大 50 要素まで(モデル混乱回避)。多い場合はビューポート内のみに絞る
- ホバーで現れる隠れ要素は対象外(視覚的に見えるもののみ)

---

## 7. 画像伝播フロー(コア設計)

### 7.1 課題
既存 `app/agent.py` の `tool_result` イベントは **文字列(text)のみ**。
Automa モードでは「ツール結果 = スクショ画像」を次の LLM ターンに渡す必要がある。
さらにステップが進むほどコンテキスト内に画像が溜まり、**KV キャッシュ肥大 / num_ctx 枯渇** が発生する。
この2課題を同時に解決する設計が必要。

### 7.2 解決方針

#### (a) `automa.py` の内部データ構造拡張
```python
class ToolResult:
    text: str
    image_b64: Optional[str] = None        # SoM 重畳スクショ
    marks: list[dict] = []                 # [{mark_id, tag, text, role, bbox}, ...]
    image_mime: str = "image/png"
    status: str = "ok"                     # ok / error / denied / timeout
```

#### (b) Ollama への次ターン投入(`automa.run_stream()` 内)
```python
# tool_result を受け取った直後、次の LLM 呼出しに画像を載せる
messages.append({"role": "tool", "name": tname, "content": tr.text})
if tr.image_b64:
    # Ollama は user/system ターンの images に画像を載せる仕様。
    # tool 役に直接 images を付けるとモデル/テンプレ依存で無視される事例があるため、
    # tool 直後に「擬似 user ターン」を挟んで画像を渡す。
    messages.append({
        "role": "user",
        "content": "(直前のツール実行後の画面。要素番号は上記要素リスト参照)",
        "images": [tr.image_b64],
    })
```

> Ollama Python SDK は `role: tool` メッセージに `images` を直接持たせる仕様が **モデル/テンプレ依存で不安定**。
> qwen2.5-vl・gemma3 の現行 chat template はいずれも「user 役の images」前提で書かれているため、
> **「user 役の擬似ターン」を挟む** のが現実的。`main.py:399-402` の既存パターンと同じ。

#### (c) DB 保存時はサムネイル化
- フル解像度(1280×800 PNG ~1MB)をすべて DB に保存するとサイズ爆発
- 履歴保存時は **WebP 圧縮で最大幅 800px、~50KB に縮小** して `messages.sources` に格納
- ライブ表示にはフル解像度をストリーミング(保存しない)
- `settings.automa.save_screenshots=false` の場合は保存自体をスキップ(機微情報対策)

### 7.3 イベントスキーマ拡張

`automa.run_stream()` が SSE で吐くイベント:

```python
{"type": "tool_call",   "name": "browser_click", "args": {...}}
{"type": "tool_result", "name": "browser_click", "status": "ok",
 "result": "要素 #login をクリックしました",
 "image_b64": "<縮小 SoM スクショ>",      # ★ 新規
 "marks": [{"mark_id": 1, ...}, ...],     # ★ 新規(SoM 要素リスト)
 "image_full_url": "/api/automa/...png"}  # ★ ライブ表示用
{"type": "screenshot",  "image_b64": "...", "marks": [...]}  # 明示の screenshot 時
{"type": "approval_required", "action_id": "...", "summary": "..."}
{"type": "ask", "action_id": "...", "question": "...", "options": [...]}
{"type": "plan", "plan": "..."}
{"type": "todos", "todos": [...]}
{"type": "done", "message": {...}}
{"type": "error", "error": "..."}
```

### 7.4 コンテキスト内画像のスライディングウィンドウ(重要)

#### 課題
擬似 user ターンの画像をそのまま積み続けると、20ステップ後にはコンテキストに
**画像20枚**(VLM では1枚あたり数百〜千トークン超)。
**`num_ctx` を即座に食い潰し、KV キャッシュも VRAM を圧迫する**。
32GB あっても画像の積み増しは線形に効くため、対策が必須。

#### 設計: 直近 N 枚のみフル画像、それ以前はテキスト要約に差替え
推論直前(=`chat()` 呼出し直前)に `messages` をプルーニングする:

```python
PRUNE_KEEP_IMAGES = 2   # 直近2ターンのみフル画像を保持(調整可能)

def prune_for_inference(messages: list[dict]) -> list[dict]:
    """擬似 user ターンの画像を、直近 N 個だけ残して他は剥がす。"""
    # 後ろから走査、画像付き擬似 user ターンを N 個まで保持
    kept_count = 0
    pruned = []
    for msg in reversed(messages):
        if msg.get("role") == "user" and msg.get("images"):
            if kept_count < PRUNE_KEEP_IMAGES:
                pruned.append(msg)
                kept_count += 1
            else:
                # 画像を剥がしてテキスト記述に置換
                marks_summary = msg.get("_marks_summary", "(過去のスクショ)")
                pruned.append({
                    "role": "user",
                    "content": f"(過去のスクショ: {marks_summary})",
                })
        else:
            pruned.append(msg)
    return list(reversed(pruned))
```

#### `_marks_summary` の生成
画像を擬似 user ターンに積む時点で、要素リストから短い説明文を生成して**同じ msg に付けておく**:
```python
def summarize_marks(marks: list[dict], url: str, title: str) -> str:
    # 例: "google.com / 検索結果ページ: 検索ボックス, 検索ボタン, 結果リンク10件"
    notable = [m["text"][:20] for m in marks[:8] if m.get("text")]
    return f"{url} / {title}: " + ", ".join(notable)
```

これにより、古いステップを**「画像」から「短いテキスト記憶」に圧縮**しつつ、
モデルは履歴全体の流れを失わずに最新画面に集中できる。

#### 補足: 完了画面の保護
最終的なゴール画面(タスク完了直前の画面)は、要約に落ちないよう
`done` イベント直前で **明示的に画像を再注入** する選択肢もあり(Phase 1.5 で検討)。

### 7.5 システムプロンプト(擬似 user ターンの解釈をモデルに明示)

擬似 user ターンの存在をモデルに認識させないと、
「ユーザが新たに画像を送ってきた」と誤解して**会話の文脈を切り替える**リスクがある。
システムプロンプトで明示する:

```text
あなたはブラウザ自動操作エージェントです。専用 Chromium 内だけで作業します。

【画面の見え方】
画面確認は screenshot ツールで取得します。
ツール結果として返る画像には、操作可能な要素に番号付き赤枠(Set-of-Marks)が描かれ、
要素リストが同時に渡されます。クリック・入力の対象は原則 mark_id で指定してください。

【会話の構造】
- "tool" 役のメッセージはツール実行結果(テキスト)です。
- "tool" 直後に「(直前のツール実行後の画面...)」という user 役メッセージが現れます。
  これは **あなたが直前に行ったツール操作の結果画面** であって、ユーザからの新規依頼ではありません。
  内容に応じて次の行動を決めてください。
- 「(過去のスクショ: ...)」は過去ステップの画面の **テキスト記憶** で、画像は省略されています。
  最新の画像と組み合わせて状況を把握してください。

【依頼の達成】
- セレクタや座標より mark_id を優先してください(精度が高い)。
- mark_id で扱えない場合のみ selector / 座標を使う。
- 完了したら、ツール呼出しを止めて日本語で結果を要約してください。
```

このシステムプロンプトは `automa.py` の `SYSTEM_PROMPT` 定数として保持する。

---

## 8. API 設計

### 8.1 新規エンドポイント

```python
# app/main.py に追加
@app.post("/api/conversations/{cid}/automa",
          dependencies=[Depends(auth.require_auth)])
def api_automa(cid: str, body: AgentBody) -> Response:
    """Automa モード実行。SSE で進捗ストリーム。"""

@app.post("/api/automa/approve", dependencies=[Depends(auth.require_auth)])
def api_automa_approve(body: ApproveBody) -> dict:
    """操作承認(Code の approve と同形式)。共通化を検討。"""

@app.post("/api/automa/answer", dependencies=[Depends(auth.require_auth)])
def api_automa_answer(body: AnswerBody) -> dict:
    """ask_user への回答。"""

@app.post("/api/automa/abort", dependencies=[Depends(auth.require_auth)])
def api_automa_abort(cid: str = Body(..., embed=True)) -> dict:
    """緊急停止: ループ中断 + ブラウザ即終了。"""

@app.get("/api/automa/{cid}/live.png",
         dependencies=[Depends(auth.require_auth)])
def api_automa_live(cid: str) -> Response:
    """ライブスクショ(現在の画面)。ポーリング用。
       Phase 1.5 で WebSocket / SSE プッシュへ移行検討。"""
```

### 8.2 状態管理

`main.py` 既存の Code エージェントに倣い、メモリ保持:

```python
_automa_ctx: dict[str, list] = {}                # 会話ごとの会話履歴(messages)
_automa_sessions: dict[str, BrowserSession] = {} # 会話ごとのブラウザ
_automa_running: set[str] = set()                # 実行中の会話 ID
_automa_lock = threading.Lock()
```

### 8.3 会話削除時のクリーンアップ
`api_delete_conversation` で Code と同様の処理を追加:
```python
with _automa_lock:
    sess = _automa_sessions.pop(cid, None)
    if sess: sess.close()
    _automa_ctx.pop(cid, None)
    _automa_running.discard(cid)
```

---

## 9. セキュリティ設計

### 9.1 操作スコープ(技術的サンドボックス)
- **専用 Chromium プロセス** を Playwright で起動。ユーザの個人ブラウザと分離
- `user-data-dir = data/automa_browser/<conv_id>/` に固定 → クッキー・ログイン情報も会話内で完結
- Phase 1 では **デスクトップに触れない** → 暴走しても被害はブラウザ内のみ

### 9.2 承認フロー
| モード | 動作 |
|---|---|
| 計画モード ON(既定) | `present_plan` で承認 → 以降の操作はクリック・入力等を**サマリ付きで承認** |
| 計画モード OFF + 各操作で確認 ON | すべての変更系操作を **個別に承認** |
| 計画モード OFF + 各操作で確認 OFF | 操作前承認なし。自動実行モード(注意喚起トースト必須) |

### 9.3 URL ホワイトリスト
- `settings.automa.allowed_domains` が空でない場合、`browser_open` / `browser_navigate` 実行前に
  ドメイン照合(`fnmatch` で `*.example.com` 形式対応)
- マッチしなければツール側で deny し、エラー結果を LLM に返す

### 9.4 緊急停止
- フロント: 赤い「■ 停止」ボタン → `POST /api/automa/abort`
- サーバ:
  1. `_automa_running` から除外
  2. SSE ストリームの `GeneratorExit` 発火(クライアント切断と同等)
  3. `BrowserSession.close()` を強制実行(Playwright `browser.close()`)
- LLM 推論中の Ollama リクエストは Ollama 側のキャンセル機構に依存(タイムアウト)

### 9.5 認証・LAN 制限
- 既存 `auth.require_auth` をそのまま適用
- `LAN_ONLY` ミドルウェアも自動的に適用される(`main.py:82-93`)
- README に「Automa モードはサーバ上でブラウザを起動するため、Code と同様に **必ず `CHAT_PASSWORD` 設定**」を追記

### 9.6 ログ
- 操作前後のスクショ + ツール呼出し引数を構造化ログに出力
- 監査用に `data/automa_audit/<conv_id>/<timestamp>.json` へ追記(オプション・既定 OFF)

### 9.7 ephemeral モード(機微サイト用)

#### 課題
既定では `user-data-dir = data/automa_browser/<conv_id>/` に Cookie / セッション情報を永続化する。
これは「再アクセス時の再ログイン不要」という UX には利点だが、
**銀行・行政・社内基幹システム等の機微サイト**では「ログイン状態を保存しない」運用が必要。
これらの Cookie が `data/` 配下に平文で残ることはセキュリティ上のリスクになりうる。

#### 設計: 会話設定の `ephemeral` フラグ
`settings.automa.ephemeral = true` の場合:
1. `user-data-dir` を `tempfile.mkdtemp(prefix="automa_eph_")` に切替(OS の一時領域)
2. **セッション終了時に強制削除**(`browser.close()` フック + `shutil.rmtree`)
3. プロセス異常終了時のクリーンアップ用に、起動時 `data/automa_browser/` 配下の orphan(`eph_` 前缀)を一括削除
4. UI のステータス表示に「🔒 機微モード(履歴非保存)」を表示

#### 既定値・運用
- **既定は `false`**(利便性優先)
- 会話作成時 / 設定画面でトグル可
- `ephemeral=true` のときは `save_screenshots` も自動で `false` 推奨(設定で警告表示)

#### スコープ外
- 完全な「シークレットモード」ではない(Playwright 自体は incognito 起動可能だが、user-data-dir 制御の方が運用が単純なので Phase 1 ではこちら採用)
- DNS キャッシュや OS レベルの痕跡は対象外

---

## 10. ファイル単位の改修一覧

### 10.1 新規ファイル

| ファイル | 内容 | 行数目安 |
|---|---|---|
| `app/automa.py` | エージェントループ・ツールスキーマ・承認解決 | ~500 |
| `app/automa_browser.py` | Playwright ラッパ。BrowserSession クラス | ~300 |
| `docs/automa_design.md` | 本設計書 | (本ファイル) |
| `docs/automa_user_guide.md` | エンドユーザ向け使い方(Phase 1 完了時) | ~150 |

### 10.2 修正ファイル

| ファイル | 修正点 |
|---|---|
| `app/main.py` | `/api/conversations/{cid}/automa` 等 5 ルート追加、`_automa_*` 状態、会話削除時クリーンアップ |
| `app/db.py` | `kind="automa"` を許容(現状でも動くが定数化推奨)。`messages.sources` の解釈はそのまま流用 |
| `app/agent.py` | 承認 resolve 関数(`resolve`, `resolve_answer`)を `automa.py` から再利用するため公開 API に格上げ。または共通モジュール `app/agent_common.py` を切り出す |
| `app/safety.py` | `is_url_allowed(url, allowed_domains)` 関数追加 |
| `app/config.py` | `AUTOMA_BROWSER_HEADLESS`, `AUTOMA_BROWSER_VIEWPORT` 等の環境変数読込追加 |
| `app/static/index.html` | mode-tabs に Automa 追加、`#automa-bar`, `#automa-screen` ペイン追加 |
| `app/static/css/style.css` | `.only-automa`, `body[data-mode="automa"]`, `.automa-bar`, `.automa-screen` 等 |
| `app/static/js/app.js` | `setMode("automa")` 対応、Automa バー操作、ライブスクショ更新、緊急停止 |
| `requirements.txt` | `playwright>=1.40`, `Pillow>=10`(スクショ圧縮用) |
| `.env.example` | `AUTOMA_BROWSER_HEADLESS=false` 等の追加 |
| `README.md` | 機能紹介セクション、起動手順(`playwright install chromium`)、安全注意 |

---

## 11. 段階計画

### Phase 1(本設計の中核 / 推定 5〜7 営業日)
- [ ] Playwright 統合 (`automa_browser.py`)
- [ ] ツール 10 個実装(`screenshot`, `browser_*`, `ask_user`, `present_plan`, `todo_write`)
- [ ] 画像伝播(ToolResult, 擬似 user ターン)
- [ ] `automa.py` のエージェントループ
- [ ] API ルート + 状態管理
- [ ] UI: Automa タブ、automa-bar、ライブスクショ(ポーリング)、緊急停止
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
| R4 | VRAM 競合(Chat + Vision + Embedding 同時常駐) | 中 | Ollama `keep_alive=5m` を `automa` 用ターン中だけ 0 にして他モデル解放。設定で切替 |
| R5 | Playwright 初回 install | 低 | README に `playwright install chromium` を明記。`run.py` 起動時に未インストール検知警告 |
| R6 | ヘッドレス vs 通常 | 中 | サーバが Linux server で GUI なしの場合 `headless=true` 必須。Windows デスクトップなら好みで選択 |
| R7 | 機微情報のスクショ保存 | 高 | 履歴サムネイルは既定 ON、設定で OFF 可。ログイン情報は `user-data-dir` 内に閉じる |
| R8 | 同時アクセス時のセッション衝突 | 低 | 会話単位で `_automa_running` で排他、別会話なら並列可 |
| R9 | LAN 公開時に他人が誤って操作 | 高 | `CHAT_PASSWORD` 必須化(README で強調)、Automa モードは認証ユーザのみ |
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
| T7 | 会話削除 | ブラウザプロセス終了、`data/automa_browser/<cid>/` クリーンアップ |
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
| Q5 | RAG 連携 | 「特定サイトの操作手順書を RAG で参照しながら Automa モードが動く」設計を将来盛り込むか |
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
| `agent.run_stream()` のループ構造 | `automa.run_stream()` |
| `agent.resolve()` / `resolve_answer()` | 承認・ask_user 解決 |
| `agent.compact_ctx_with_model()` | 文脈圧縮(長期作業時) |
| `main.py:399-402` の images 投入パターン | スクショ画像の messages 注入 |
| `main.py:_code_ctx` の状態管理パターン | `_automa_ctx` / `_automa_sessions` |
| `db.add_message` / `list_messages` | メッセージ永続化 |
| `messages.sources` の構造化ステップ保存 | スクショ + ツール記録 |
| SSE ヘルパ `sse()` | 進捗ストリーム |
| 既存 `mode-tab` / `data-mode` の CSS パターン | Automa タブ |
| `auth.require_auth` 依存 | 全 API |

---

## 17. 改訂履歴

### v0.2(本版・着手前レビュー版 2 回目)
深掘りレビューで判明した「実装すると最初に破綻する箇所」を反映:

| 改訂 | 内容 | 対応セクション |
|---|---|---|
| 1 | **Set-of-Marks(番号付き要素)方式** を採用。クリック設計の根本見直し | §6.1 / §6.2 / §6.3 / §6.5 |
| 2 | **コンテキスト内画像のスライディングウィンドウ**(直近2枚のみフル、他はテキスト要約) | §7.4 |
| 3 | **擬似 user ターンの解釈をシステムプロンプトで明示** | §7.5 |
| 4 | **ephemeral モード**(機微サイト向けに Cookie/履歴非保存)を `settings.automa.ephemeral` で導入 | §5.2 / §9.7 |
| 5 | `save_screenshots` フラグでスクショ保存自体を抑止可能に | §5.2 / §7.2(c) |
| 6 | リネーム漏れの修正(`AUTO_BROWSER_*`、`PyAutomaGUI`) | §10 / §11 |

### v0.1(初版)
- 設計の骨格を確定(命名・タブ位置・モジュール構成・既存資産との分担・段階計画・受入テスト)

---

**設計書 v0.2 / 着手前レビュー版**
