# 設計書: Cowork機能 ステップ1 — ①使用可能アプリ(コマンド)許可リスト / ③自律デバッグループ

> **状態: ドラフト(設計のみ・導入は見送り中)。** 本書は実装前の設計でありコードはまだ無い。
> 着手を再開するときは **§7 再開チェックリスト** から入る。最終更新: 2026-06-09。

社内文書アシスタントの「Cowork(AIと協働して作業)」拡張の **第一弾** 設計。
本書は **① 許可リスト** と **③ 自律デバッグループ** に限定する。
②コンピュータ操作(スクショ操作)・④ブラウザ操作は **§6** に位置づけのみ記す。

- 前提: 完全ローカル/オフライン厳守・`LAN_ONLY`・単一プロセス(`uvicorn --workers=1`)・**既存挙動を壊さない**
- 関連: [architecture.md](../architecture.md) §3(エージェント)、[code-roadmap.md](../code-roadmap.md)、[CONTRIBUTING.md](../../CONTRIBUTING.md)

### 目次
0. 背景と位置づけ(Coworkクラスとの差分・対象外)
1. なぜ①③から
2. 現状の基盤(再利用ポイント)
3. 機能①: 使用可能アプリ(コマンド)許可リスト
4. 機能③: 自律デバッグループ
5. 決定事項(暫定)/ マイルストーン / リスク
6. 位置づけ(②④は別書)
7. 再開チェックリスト

---

## 0. 背景と位置づけ(Coworkクラスとの差分・対象外)

本拡張は、クラウド型の "AIコワーカー"(Devin / Cursor / Cline / Claude の computer・browser use 等、
フロンティアモデルで自律作業するツール群。以下 **「Coworkクラス」**)の機能を、
**オンプレ・ローカルLLM・承認制**で再現する試み。差分の要点:

| 観点 | Coworkクラス(クラウド) | 本拡張(オンプレ) |
|---|---|---|
| 実行場所 | クラウドのサンドボックス/VM | **サーバPC**(アプリ稼働機)上 |
| 頭脳(モデル) | フロンティア(Claude/GPT) | **ローカル Ollama**(`gemma3:27b` 等) |
| データ | コード/文脈をクラウド送信 | **完全ローカル・外部送信なし** |
| 自律度 | 高(長時間・多段を自走) | **承認制・上限付きループ**(有界) |
| 強み | 操作精度・自律度・なめらかさ | **閉域・データ非送出・無料ローカル・社内文書RAGと一体** |

→ **設計の指針**: 「操作のなめらかさ」より **「安全・閉域・有界(暴走しない)」** を優先する。
これが ①(許可リスト)と ③(上限付き・承認制ループ)の設計根拠であり、Coworkクラスとの最大の思想差。

**本書の対象外(やらないこと)**
- 敵対的回避まで防ぐ完全サンドボックス(コンテナ隔離は将来課題)。本書の許可リストは**ガードレール**。
- 公開インターネットへの自由アクセス(オフライン方針を維持)。
- ②コンピュータ操作・④ブラウザ操作の詳細設計(別書)。

---

## 1. なぜこの2つを最初にやるか

| | 効果 | リスク | 既存資産の再利用 |
|---|---|---|---|
| ① 許可リスト | ②③④すべての**安全弁**になる土台 | 低 | `safety.py` の許可/拒否思想、`_change_action` の承認分岐 |
| ③ 自律デバッグループ | 「コードの完成度を上げる」を**直接**実現。低リスク高効果 | 低 | `run_stream` ループ・承認・中止フラグ・SSE |

①は②④の前提(どのアプリ/コマンドを動かしてよいかの制御)。③は既に**半分は挙動として存在**する
(SYSTEM_PROMPT に「緑になるまで繰り返す」指示: `app/agent/constants.py:68`)ため、明示機能化の費用対効果が高い。

---

## 2. 現状の基盤(再利用ポイント)

実装は新規構築ではなく、既存のエージェント基盤への**差し込み**で実現する。

- **ツール実行**: `app/agent/tools.py`
  - `t_run_command`(同期、`subprocess.run(shell=True, cwd=ws)`、`CMD_TIMEOUT=120`)
  - `t_run_background` / `_bg_reader` / `t_command_output` / `t_stop_command`(`_bg_jobs` + `_bg_lock`、`MAX_BG_JOBS=10`)
  - `dispatch(ws, name, args)`(`tools.py:327`): ツール名→実装の振り分け
- **承認ステートマシン**: `app/agent/_impl.py`
  - `_change_action(name, plan_mode, phase, allow_changes, auto_accept_edits)`(`_impl.py:150`):
    `block` / `confirm`(差分つき確認)/ `apply`(自動適用)を返す
  - `CONFIRM_IN_EXEC = {"run_command","run_background"}`(`constants.py:284`): 計画承認後でも確認する重要操作
- **安全管理**: `app/safety.py`(作業フォルダの許可/拒否。`check_workspace` / `is_within_protected`)
  → 「許可/拒否を一元判定して**理由文字列**を返す」契約を**コマンドにも横展開**する(§3.4 の `cmdguard`)
- **設定**: `app/config.py`(`Settings`。例: 埋め込み設定 `config.py:80-88`)+ 設定UI(`index.html` の ⚙ パネル)
- **中止フラグ + バックグラウンド + SSE 進捗**: 一括要約が実例
  (`/api/indexes/{iid}/summarize`(同期SSE)/ `/summarize/start`(裏)/ `/summary/cancel` → `index_service.request_cancel`)。
  ③のループ制御(中止可能・進捗配信)はこの構造を踏襲する。

---

## 3. 機能①: 使用可能アプリ(コマンド)許可リスト

### 3.1 要件
- 管理者が「エージェントが実行してよいアプリ/コマンド」を**許可リスト**で制御できる。
- リストに無い実行ファイルを使うコマンドは、承認カードに出す前に**ブロック**(理由を表示)。
- **後方互換**: 既定は「制限なし(=現状どおり、コマンドは毎回承認)」。明示的にONにしたときだけ適用。
- 許可リストは「実行ファイル名」単位(例: `python`, `pip`, `npm`, `node`, `git`, `pytest`, `ruff`, `tsc`)。

### 3.2 データモデル(config)
`app/config.py` に追加(`.env` / 環境変数):

| 設定 | 既定 | 意味 |
|---|---|---|
| `AGENT_CMD_ALLOWLIST_ENABLED` | `false` | true で許可リストを強制(false=現状互換) |
| `AGENT_CMD_ALLOWLIST` | 初期セット(§5 決定) | 許可する実行ファイル名(カンマ区切り)。`enabled` のときだけ効く |
| `AGENT_CMD_DENYLIST` | 既定の危険語 | 常に拒否(例: `rm,del,format,shutdown,reg,diskpart,mkfs`)。allowlist より優先 |

- denylist は `enabled` に関係なく**常に有効**(最低限の安全網)。allowlist は `enabled` 時のみ。
- 将来: 会話単位の上書き(`settings` JSON 列)も可能だが、**第一弾はグローバル設定のみ**(乱用防止)。

### 3.3 コマンド解析の方針(設計上の肝)
`shell=True` のため、コマンドは任意のシェル文字列(パイプ・`&&`・`;`・リダイレクト)になりうる。
完全パースは不可能なので**保守的**に判定する:

1. `&&` `||` `|` `;` `&` でセグメント分割。
2. 各セグメントを `shlex.split()`(Windows は `posix=False`)し、**先頭トークン=実行ファイル**を取り出す。
3. 実行ファイル名を正規化(ディレクトリ除去・`.exe`/`.cmd`/`.bat` 除去・小文字化)。
4. denylist に1つでも該当 → **拒否**。allowlist 有効時、**全セグメント**の実行ファイルが allowlist に含まれなければ **拒否**。
5. パース不能(クォート不整合等)→ 安全側に倒して**拒否**(理由: 解析不能)。

> 注: これは「うっかり/明らかな逸脱」を止める**ガードレール**であり、敵対的回避を完全に防ぐサンドボックスではない
> (§0 対象外)。本質的な隔離が要るなら将来コンテナ実行を別途検討。

### 3.4 強制ポイント(多層防御)と インターフェース
| 層 | 場所 | 役割 |
|---|---|---|
| ハードゲート | `tools.py` `t_run_command`/`t_run_background` 冒頭 | 承認を通っても、許可外なら `[エラー] …許可リスト外` を返し**実行しない** |
| 事前判定 | `_impl.py` `_change_action`(または preview 生成時) | 許可外コマンドは `confirm` ではなく **`block`** にし、承認カードを出さず理由を表示 |

新規ヘルパ `app/agent/cmdguard.py`(純関数・`config` のみ依存。`safety.py` と同じ「(ok, 理由)」契約):
```python
def check_command(command: str) -> tuple[bool, str]:
    """コマンド文字列を実行してよいか判定。戻り: (ok, 理由)。
    - denylist は常時適用 / allowlist は AGENT_CMD_ALLOWLIST_ENABLED のときだけ適用
    - ok=False の理由はそのまま利用者向けの [エラー] 表示に使う
    """

def _executables(command: str) -> list[str]:
    """シェル文字列を保守的に分解し、各セグメントの先頭実行ファイル名(正規化済み)を返す。
    解析不能なら空 list ではなく例外でなく『不明』マーカーを返し、呼び出し側で拒否に倒す。"""
```
`tools.py` と `_impl.py` の両方から `check_command` を呼ぶ(**単一の真実**)。

### 3.5 UI
- 設定モーダル(⚙)に「エージェントの実行許可」セクション: ON/OFF トグル + 許可リスト(チップ入力)+ 拒否リスト(読み取り専用表示)+ **「これはサンドボックスではない」旨の注記**。
- 値は `/api/config`(既存の設定取得/保存系)に乗せる。許可外がブロックされたら、ツール結果の `[エラー]` 表示に理由が出る。

### 3.6 テスト(`tests/test_cmdguard.py`、独自ランナー形式)
- `python tests/run_all.py` で回る **pytest 非依存**形式(`__main__` 単体実行可)。
- ケース: 単純コマンド許可/拒否、`a && b` の複合、パイプ、`rm -rf`(denylist)、クォート不整合(拒否)、
  Windows風 `C:\\…\\python.exe` の正規化、`enabled=false` で素通り(後方互換)。

### 3.7 受け入れ基準
- `enabled=false` で**現状と完全に同一挙動**(既存テスト緑)。
- `enabled=true` + allowlist=`python,pytest` のとき、`pytest -q` は通り `curl …` はブロックされ理由表示。
- denylist は `enabled` に関係なく `rm`/`del` 等を拒否。

---

## 4. 機能③: 自律デバッグループ(検証→修正→再検証)

### 4.1 要件
- 「検証コマンド(テスト/ビルド/型/lint)を実行 → 失敗を解析 → 修正 → 再実行」を**自動で繰り返す**。
- **上限N回**(既定4、設定可)/ **中止可能** / 各修正の差分は**承認制(既定)** または自動適用(トグル)。
- 収束(=検証成功)または「N回到達 / 進展なし / 外部要因」で停止し、**結果を要約**。

### 4.2 既存挙動との差分(何を新設するか)
- 既存: エージェントは `run_command` で検証でき、プロンプトに「緑まで繰り返す」指示あり(`constants.py:64-70`)。
  だが**ループの保証が無い**(モデルが途中で止める/検証を省く/上限が曖昧)。
- 新設: **オーケストレーション層**が検証コマンドを決定的に回し、ループ回数・中止・進捗・停止条件を**機構として保証**する。
  LLM は「失敗ログ→修正案(`edit_file`/`write_file`)」の部分だけを担当する。

### 4.3 アーキテクチャ と インターフェース
```
[新] app/services/debugloop.py(オーケストレータ)
   ├─ 1) 検証コマンドを決定(自動検出 or 指定)→ ①の check_command を通す
   ├─ 2) verify 実行(tools.t_run_command 再利用、終了コード+出力)
   ├─ 3) 成功? → 終了(成功要約)
   ├─ 4) 失敗 → 失敗ログ+対象を LLM へ → ツール(edit_file/write_file)で修正
   │        └─ 修正は _change_action の承認ポリシーに従う(差分承認 or auto-accept)
   ├─ 5) iter++ / 中止フラグ確認 / 進展判定(検証出力ハッシュが不変なら「進展なし」)
   └─ 6) 上限到達 or 進展なし or 中止 → 停止(理由つき要約)
```
実装形態は **独立 `debugloop` 関数が `run_stream` を内部利用**(§5 決定。テスト容易・保守性):
```python
# app/services/debugloop.py
def run_debug_loop(cid: str, ws: Path, *, verify_cmd: str | None = None,
                   max_iters: int = 4, auto_accept_edits: bool = False,
                   model: str) -> Iterator[dict]:
    """検証→修正→再検証を上限内で繰り返し、進捗 dict を yield する(SSE 化は routes 層)。"""
```

### 4.4 SSE イベント(`iter_summary_sse` と同型。`type` で分岐)
```json
{"type":"loop_start","verify":"pytest -q","max_iters":4}
{"type":"loop_iter","iter":1,"max":4}
{"type":"verify","iter":1,"ok":false,"code":1,"tail":"...末尾ログ(数百字)..."}
{"type":"fix","iter":1,"files":["app/x.py"]}
{"type":"loop_done","ok":true,"iters":2,"reason":"verify_passed","summary":"..."}
```
`reason ∈ { verify_passed, max_iters, no_progress, canceled, blocked }`。

### 4.5 検証コマンドの決定
1. **自動検出**(read_file/grep): `package.json` の `scripts.test`/`build`、`pyproject.toml`/`pytest.ini`、`Makefile`、`tox.ini` 等。
   候補(例: `pytest -q` / `npm test` / `npm run build` / `tsc --noEmit` / `ruff check` / `go build ./...`)。
2. **ユーザー指定**(UI入力)で上書き可。
3. 「VSCodeのログ」要件について: `%APPDATA%\\Code\\logs` の読取も可能だが、**実際に直すのに有効なのはコンパイラ/テストの出力**。
   第一弾は **検証コマンドの stdout/stderr を一次信号**にする(VSCodeログ読取は将来オプション)。

### 4.6 ループ制御・並行性・中止
- パラメータ: `max_iters`(既定4)、`per_cmd_timeout`(既定 `CMD_TIMEOUT`)、`auto_accept_edits`(既定 false=差分承認)。
- **中止**: 一括要約の `request_cancel`/キャンセルフラグと同じ仕組みを会話単位に用意(`_loop_cancel[cid]`)。
- **並行性**: 長時間ブロッキングのため、既存の生成と同様にワーカースレッド/ストリームで実行。単一プロセス前提は崩さない。
- **停止条件**(いずれか): 検証成功 / `max_iters` 到達 / 連続で**検証出力ハッシュ不変**(進展なし)/ 中止 / 許可外コマンドで検証不能。

### 4.7 承認・安全
- 検証コマンドは **①の `check_command`** を通す(test ランナーやビルドツールは allowlist 初期セットに含む)。
- 各修正(`edit_file`/`write_file`)は既定で**差分承認**。`auto_accept_edits` を入れると編集は自動適用、ただし
  `CONFIRM_IN_EXEC`(コマンド)は引き続き確認(既存方針 `_impl.py:158-166` を尊重)。
- 作業フォルダ外は不可(既存 `_safe_path`)。

### 4.8 UI
- Codeタブに「🔁 デバッグループ」起動 + 検証コマンド入力(自動検出値をプレフィル)+ 上限回数 + 自動適用トグル + 中止ボタン。
- ループ進捗は思考/ツールログと同様にストリーム表示(回数・各検証の合否・変更ファイル)。

### 4.9 テスト(`tests/test_debugloop.py`)
- 検証コマンド自動検出のパース、停止条件(成功/上限/進展なし)、中止フラグ、許可外で停止。
- LLM 呼び出しは**スタブ注入**(独自ランナーは重い ML/LLM を呼ばない方針に合わせ、`fn` を差し替えてオフライン検証)。

### 4.10 受け入れ基準
- わざと1テストを落としたサンプルで起動 → エージェントが修正 → **緑で停止**し要約を出す(ローカルLLM・ツール対応モデル前提)。
- 直らないケースで `max_iters` 到達 → 「未解決」と原因仮説を返して停止(**無限ループしない**)。
- 中止ボタンで即停止。allowlist に検証ツールが無いと `reason=blocked` で安全に停止。

---

## 5. 決定事項(暫定)/ マイルストーン / リスク

### 決定事項(暫定・再開時に再確認可)
かつての「未決」を、Coworkクラスとの差分(§0 指針=安全・有界優先)に沿って暫定決定:

| 論点 | 決定(暫定) | 根拠 |
|---|---|---|
| 許可リスト既定値 | **主要開発ツールの初期セット**を同梱: `python,pip,pytest,ruff,mypy,node,npm,npx,git,go,tsc`(ただし `ENABLED=false` が既定なので最初は無効) | ON にした瞬間に③が回る実用性。後方互換は `ENABLED` で担保 |
| ③ 編集の既定 | **毎回差分承認**(auto-accept はトグルでオプトイン) | 安全優先・既存方針(`_change_action`)と一貫 |
| ③ 実装形態 | **独立 `debugloop` 関数(`services/debugloop.py`)が `run_stream` を内部利用** | テスト容易・`routes`→`services`→エンジンの層を維持 |

### マイルストーン
1. **M1 ①許可リスト**: `config.py` 設定 + `cmdguard.py` + `tools.py`/`_impl.py` 強制 + テスト + UI。
2. **M2 ③ループ中核**: `debugloop` オーケストレータ + 停止条件 + テスト(LLMスタブ)。
3. **M3 ③統合**: 検証コマンド自動検出 + SSE進捗 + UI + 中止。
4. **M4 仕上げ**: ドキュメント(README/architecture)更新、CI緑、`.env.example` 追記。

各 M は「**先にテスト→実装→ruff/mypy/テスト緑→コミット**」(CONTRIBUTING 準拠)。M1 と M2 は独立に着手可能。

### リスク
- **コマンド解析の限界**(§3.3): ガードレールであって完全サンドボックスではない旨を明記・UIにも注記。
- **ローカルLLMの修正力**: ツール対応モデル(`qwen3` 等)前提。弱いモデルだと収束しない → `max_iters` で安全停止。
- **長時間処理**: タイムアウト・中止・進展なし検出で暴走防止。

---

## 6. 位置づけ(②④は別書)
- **② コンピュータ操作(スクショ→操作)**: `mss`/`pyautogui`+Vision。座標グラウンディングがローカルVLMの限界に直結。
  Windows UI Automation での要素座標取得が鍵。**①の許可リストと承認制が前提**。
- **④ ブラウザ操作**: Playwright(DOM/アクセシビリティ駆動が確実)。依存重・ネットワーク方針(許可URL)要。
- いずれも「操作対象=サーバPC」。本書①③が**安全基盤**となる。

---

## 7. 再開チェックリスト(導入を再開するとき)
1. §5 の「決定事項(暫定)」を**確定**する(初期 allowlist の中身・編集の既定・実装形態)。
2. **M1 から着手**: `tests/test_cmdguard.py` を**先に**書く → `cmdguard.py` 実装 → `tools.py`/`_impl.py` に `check_command` を差し込む → `ruff`/`mypy`/`run_all.py` 緑 → コミット。
3. M2/M3 で ③: `tests/test_debugloop.py`(LLMスタブ)→ `services/debugloop.py` → routes/SSE → UI → 中止。
4. `.env.example` と README/architecture に新設定・新機能を追記(M4)。
5. ①③が安定したら、②④の設計書に進む(本書 §6)。
