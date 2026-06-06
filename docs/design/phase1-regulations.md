# Phase 1 詳細設計:社内規定の構造化と相互参照の解決

対象: RAG_Chatbot を「複雑な社内規定」に強くする最初の段階。
目的は **検索精度の底上げ**(条文の取りこぼし防止・参照先の自動補完・用語定義の接地)であり、
保存モデルの変更(版管理)や新機能(整合性チェック)は含めない。

- ステータス: 設計(未実装)
- 依存: 既存の見出し対応チャンク `splitter.split_structured` とハイブリッド検索 `retrieval.rerank`
- 影響範囲: 再インデックスが必要。DB スキーマ変更は**なし**(メタは Chroma 側)。`offline / LAN_ONLY` は維持。

---

## 0. 実測検証(設計の裏取り)

代表的な就業規則サンプル(章/条/項/号・漢数字・別表・附則・前条/別表参照)を、
現状の `split_structured` に通して設計の前提を**実チャンクで確認**した。

### 成立した前提(そのまま採用)
- 章・条の見出し検出は機能(`第2章 人事 > 第12条(休職)` の見出しパスを生成)。
- 漢数字の条(`第十三条`)も検出 → `kanji_to_int` は必須。
- 定義条は見出しの「定義」で判定可(`第2条(定義)`)。
- 参照は**チャンク本文に出現**し抽出可能(`前条` / `前項` / `別表第2` / `第15条`)。

### 崩れた前提(設計を修正)
- **附則・別表が直前の条に飲み込まれる。** `附則` も `別表第1` も行頭が「第」で始まらず
  見出し未検出となり、`第十三条` のチャンク本文に混入した(実測):
  ```
  PATH: '第十三条(復職)'
  BODY: '前条の…復職を命ずる。\n\n附則\n1 …施行する。\n…\n別表第1(第3条関係)\n| 書類 | … |'
  ```
  → このままでは「別表第2参照」を突合する**別表チャンクが存在せず**、附則(改訂履歴)も
  最後の条に紛れる。
- **前項/次項は同一条内**にあり、チャンク跨ぎの展開は不要(同じチャンクに含まれる)。

### 設計への反映(以降の章に織り込み済み)
1. `splitter._heading` に **附則 / 別表(第N)/ 様式(第N)/ 別記 / 別紙** を見出しとして追加。
   → 別表が独立チャンク化し、附則も分離(Phase 3 の改訂履歴抽出も容易になる)。
2. メタに **`appendix_label`(別表第1 等)** を追加し、別表参照を突合可能に。
3. 参照展開の対象は **第N条 / 別表第N / 前条 / 次条** とし、**前項/次項は除外**(条内)。
4. 長い条は複数チャンク化するため、参照取得は同一 `article_label` を**まとめて**返す。

### 実規定サンプルでの最終検証(受領待ち・実装前に実施)

> ステータス: **実サンプルは後日提供予定**(就業規則・各課規定など数本)。社内規定は
> フォーマットの癖が強いため、§0 の合成サンプルだけで確定せず、**実物でチャンク出力を
> 実測してから実装に入る**こと。機微情報はダミー化して可。

**実施手順**: 各ファイルを `loaders.load_file` → `splitter.split_structured` に通し、
`(見出しパス, チャンク本文)` の一覧を目視する(§0 と同じ要領)。

**確認チェックリスト**(崩れがあれば `splitter._heading` の正規表現・`refs.parse_article` を調整):
- [ ] 章/節/款/条/項/号 の見出し**検出漏れ・誤検出**
- [ ] 漢数字(第十二条)・全角(第１２条)・**枝番(第12条の2)**
- [ ] 附則 / 別表(第N)/ 様式 / 別記 / 別紙 が**独立チャンク**になっているか(§0 の修正が効くか)
- [ ] 定義条の見出し判定(用語の定義)
- [ ] 参照(第N条 / 別表第N / 前条 / 次条 / 前項)が**本文に出現**し抽出可能か
- [ ] 1条が複数チャンクに割れる**頻度・境界**(意味の途中で切れていないか)
- [ ] 別表(表)の素テキスト化品質
- [ ] フォーマットの癖: **ヘッダ/フッタ・ページ番号・改ページ**の混入、縦書きPDF、章番号の表記ゆれ、スキャンPDF(要OCR)

**受領後の反映**: 実測結果と必要な正規表現の追記を §0 と該当章に追記し、テスト(`test_splitter`/`test_refs`)へ**匿名化した最小例**を追加する。

**機微情報の扱い**: 検証は**ローカル処理のみ**(offline/LAN_ONLY を維持)。実規定の本文は
**リポジトリにコミットしない**。テストに載せるのは社内情報を含まない**ダミーの最小例**に限る。

---

## 1. スコープ

| 含む(Phase 1) | 含まない(Phase 2/3) |
|---|---|
| 条番号の構造化メタ(`article_no` / `article_label`) | 所管部署・規定種別メタ、整合性チェック(Phase 2) |
| 相互参照の解決(第N条 / 別表第N / 前条・次条・前項) | 版管理・改訂日・条文差分・履歴UI(Phase 3) |
| 定義条(用語の定義)の常時同梱(ピン留め) | 施行日による現行版フィルタ(Phase 3) |
| 出典への条番号反映、参照展開の明示 | |

### 受け入れ基準(Definition of Done)
1. 「第12条を見せて」で当該条文が確実に上位に来る。
2. 第12条が「第3条に定める…」を含む場合、回答の文脈に**第3条も自動同梱**される(同一規定内)。
3. 「ピン留め」有効時、用語が出る質問で**定義条が文脈に入り**、定義に沿った回答になる。
4. 既存インデックスは再索引するまで従来動作(キー欠如で例外を出さない)。
5. 規定モード OFF のとき、現行の検索結果と**完全に一致**する(無影響)。

---

## 2. データモデル

### 2.1 Chroma メタデータ(チャンク単位)— 追加キー
`rag.build_index` / `rag.add_attachment` で付与する。既存キーは不変。

| キー | 型 | 例 | 用途 |
|---|---|---|---|
| `article_no` | int | `12` | 並べ替え・相対参照の解決(前条/次条) |
| `article_label` | str | `"第12条"` | 絶対参照の突合キー・出典表示 |
| `appendix_label` | str | `"別表第1"` | 別表/様式参照(別表第N)の突合キー(§0で判明) |
| `is_definition` | bool | `true` | 定義条のピン留め対象 |

- 既存: `source, loc, path, sig, heading, attachment`(維持)。
- DB(`indexes` テーブル)変更なし。Phase 1 はメタのみで完結。

### 2.2 設定
`app/config.py`(env 既定 OFF=現行動作を変えない):

```
REGULATION_MODE   (bool, 既定 false)  # 既定の規定モード
REG_EXPAND_REFS   (bool, 既定 true)   # 規定モード時の参照展開
REG_PIN_DEFS      (bool, 既定 true)   # 規定モード時の定義ピン留め
REG_MAX_EXPAND    (int,  既定 5)      # 1リクエストで展開する参照の上限
REG_EXPAND_BUDGET (int,  既定 4000)   # 参照展開＋定義に割く文脈の文字上限
```

会話単位の上書きは `defaults.base_defaults()` に `regulation_mode`(bool)を追加し、
`effective_for` で既存同様にマージ。UI のトグルで会話ごとに ON/OFF。

---

## 3. モジュール構成

### 3.1 新規 `app/refs.py`(純粋関数・単体テスト可)
```python
# 条番号
ARTICLE_LABEL_RE  # 第12条 / 第12条の2 / 第１２条(全角)
def kanji_to_int(s: str) -> Optional[int]                 # 漢数字・全角数字 → int
def parse_article(heading_path: str) -> dict
    # -> {"article_no": int|None, "article_label": str|None, "is_definition": bool}
    # heading_path の最も深い「第N条」を採用。定義は見出しに 定義/用語 を含むか

def is_definition_heading(heading: str) -> bool

# 参照
class Ref(NamedTuple): kind: str; label: str   # kind: "article"|"appendix"|"relative"
def find_references(text: str) -> list[Ref]
    # 絶対: 第3条 / 第3条第2項 / 別表第2 / 様式第1
    # 相対: 前条 / 次条 / 前項 / 本条 / 同条
def resolve_relative(rel: str, current_no: Optional[int]) -> Optional[str]
    # 前条->第(n-1)条 / 次条->第(n+1)条 / 本条・同条->現在条。第1条の前条は None

def collect_targets(hits: list[dict], *, max_refs: int) -> list[tuple[str, str]]
    # 各 hit の本文から参照を抽出し、(source, article_label) の重複なしリストを返す
    # 相対参照は hit の article_no で解決。same-source 厳守(後述)
```

設計判断:**参照は必ず同一規定(同じ `source`/`path`)内で解決**する。
「第3条」はどの規定にも存在するため、参照元チャンクの `source` に限定しないと別規定の条文を誤って引く。

### 3.2 既存ファイルの変更
| ファイル | 変更 |
|---|---|
| `app/refs.py` | 新規(上記) |
| `app/splitter.py` | **(§0で判明)** `_heading` の規則に **附則 / 別表(第N)/ 様式(第N)/ 別記 / 別紙** を追加。別表を独立チャンク化し、附則を直前の条から分離する。`split_structured` の戻り値に別表ラベルを載せられるよう拡張(または `refs.parse_article` 側で見出しから判定) |
| `app/rag.py` | `build_index`/`add_attachment`: `refs.parse_article(heading)` の結果(`article_no/label`・`appendix_label`・`is_definition`)をメタに追加。`retrieve`: 参照展開・定義ピン留めの後処理(下記フロー)。引数 `expand_refs: bool=False, pin_definitions: bool=False` を追加 |
| `app/config.py` | `REGULATION_MODE` 他フラグを追加 |
| `app/defaults.py` | `base_defaults` に `regulation_mode`、`effective_for` でマージ |
| `app/main.py` | `api_generate`: 会話設定 `regulation_mode` を読み、`rag.retrieve(..., expand_refs=, pin_definitions=)` に反映。出典(`sources`)に `article_label` と「参照展開/定義」の別を載せる |
| `app/static/*` | 「規定モード」トグル(参照展開・定義同梱の ON/OFF)。出典バッジに「参照」「定義」を表示 |
| `tests/test_refs.py` | 新規(純粋ロジックの単体テスト) |

---

## 4. アルゴリズム

### 4.1 索引時:規定メタの付与(`rag.build_index`)
```
for chunk, heading in split_structured(b["text"], cs, co):
    a = refs.parse_article(heading)                 # {article_no, article_label, is_definition}
    doc = f"{heading}\n{chunk}" if heading else chunk
    loc = f"{b['loc']} / {heading}" if heading else b["loc"]
    meta = {... 既存 ..., "article_no": a["article_no"] or -1,
            "article_label": a["article_label"] or "",
            "is_definition": bool(a["is_definition"])}
```
- Chroma メタは None 不可のため `article_no` 欠如は `-1`、`article_label` は `""`。

### 4.2 検索時:参照展開＋定義ピン留め(`rag.retrieve` 後処理)
```
hits = retrieval.rerank(query, raw_hits, top_k, ...)        # 既存(主結果)
extra = []
if expand_refs:
    targets = refs.collect_targets(hits, max_refs=REG_MAX_EXPAND)  # [(source, "第3条"), ...]
    for source, label in targets:
        # 同一 source 内の該当条文を Chroma の where で取得(無ければスキップ)
        got = _fetch_by_article(col_of(source), source, label, limit=2)
        extra += _mark(got, note=f"参照: {label}")
if pin_definitions:
    for source in {h["source"] for h in hits}:           # 出てきた規定の定義条だけ
        defs = _fetch_definitions(col_of(source), source, limit=2)
        extra += _mark(defs, note="定義")
# 既出を除外し、予算内で hits の後ろに付加(主結果を圧迫しない)
return hits + _dedup_against(hits, _budget(extra, REG_EXPAND_BUDGET))
```
- `_fetch_by_article`: `col.get(where={"$and":[{"source":source},{"article_label":label}]})`。
- 別表参照は `appendix_label` で突合: `where={"$and":[{"source":source},{"appendix_label":label}]}`。
- `_fetch_definitions`: `where={"$and":[{"source":source},{"is_definition":True}]}`。
- **前項/次項は展開しない**(同一条チャンク内にあるため。§0)。展開対象は 第N条 / 別表第N / 前条 / 次条。
- 長い条は複数チャンクに分かれるため `limit` は 1 ではなく数件とし、同一 `article_label` を**まとめて**返す。
- 取得結果は hit 互換の dict（`text/source/loc/...`）へ整形し、`loc` に `note` を反映(例 `第3条(参照)`)。
- **予算管理**: `extra` は合計 `REG_EXPAND_BUDGET` 文字まで。`build_context_block` の全体上限(12000字)とは別の内枠。

### 4.3 出典表示(`main.py` / フロント)
- `sources[]` に `article_label` と `kind`(`primary` / `reference` / `definition`)を追加。
- フロントの出典バッジで「参照」「定義」を淡色チップ表示し、主結果と区別。

---

## 5. 移行・再インデックス手順
1. 追加メタは (再)索引時のみ付く。**既存インデックスは「↻ 再構築」で反映**(キー欠如時は `-1`/`""`/`false` 既定で安全)。
2. e5-base 化・見出し対応チャンクの再索引と**同時に実施**すれば追加コストはほぼ無い。
3. ロールアウト:
   - 既定は `REGULATION_MODE=false`(無影響)。
   - 規定フォルダを使う会話だけ UI トグルで ON。
4. ロールバック: トグル OFF または env false で即座に従来動作へ。コード/データの破壊なし。

---

## 6. エッジケースとリスク
| 事項 | 対応 |
|---|---|
| 「第3条」は全規定に存在 | **same-source 限定**で解決(別規定を引かない) |
| 前条/次条が境界外(第1条の前条) | `resolve_relative` が None を返しスキップ |
| 参照の連鎖(第3条→第2条…) | Phase 1 は**1ホップのみ**(展開結果からの再展開はしない) |
| 過剰展開(多数の条を引用) | `REG_MAX_EXPAND` と文字予算で上限 |
| 前項/次項 | 同一条チャンク内のため**展開しない**(§0 実測) |
| 別表参照(別表第2) | `splitter` で別表を独立チャンク化し `appendix_label` で突合(§0)。PDF はラベルが不安定 → 取れた分のみベスト・エフォート |
| 附則が条に混入 | `splitter._heading` に 附則 を追加して分離(§0)。Phase 3 の改訂履歴抽出の前提にもなる |
| 定義の誤検出 | 本文ではなく**見出し**に 定義/用語 を含むかで判定 |
| 性能(参照ごとの Chroma 取得) | 件数を上限化。必要なら per-request で `source` 単位のラベル→ids を1回だけキャッシュ |
| 漢数字/全角(第十二条/第１２条) | `kanji_to_int` で正規化 |

---

## 7. テスト計画(`tests/test_refs.py` ＋ `tests/test_splitter.py` 拡張、外部依存なし)
- `kanji_to_int`: 「十二」→12 / 「１２」→12 / 「3」→3。
- `parse_article`: 「… > 第12条 基本給」→ 12 / "第12条" / is_def=False。「… > 第2条 定義」→ is_def=True。
- `is_definition_heading`: 「第2条 用語の定義」True / 「第3条 給与」False。
- `find_references`: 「第3条に定める」→ article 第3条 / 「別表第2」→ appendix / 「前項のとおり」→ relative。
- `resolve_relative`: 前条@第3条→第2条 / 次条@第3条→第4条 / 前条@第1条→None。
- `collect_targets`: 相対参照を hit の `article_no` で解決し、same-source の `(source,label)` を重複なく返す。第1条の前条は除外。前項/次項は対象外。
- **`split_structured`(§0回帰)**: 附則・別表第N が**独立チャンク**になり、直前の条に混入しない。別表チャンクから `appendix_label` を取得できる。
- 統合(モック lookup): 参照展開が既出を除外し予算内に収まる。

> retrieve 実体(Chroma/埋め込み)は本コンテナで実行不可のため、純粋ロジックを単体テストし、実検索は手元で `evalkit/run_eval.py` で確認する。

---

## 8. 見積り(ファイル別の規模感)
| ファイル | 追加/変更 | 規模 |
|---|---|---|
| `app/refs.py` | 新規(条番号・参照・解決) | 中 |
| `app/rag.py` | メタ付与 + retrieve 後処理 | 中 |
| `app/config.py` / `defaults.py` | フラグ追加 | 小 |
| `app/main.py` | フラグ反映 + 出典拡張 | 小 |
| `app/static/*` | 規定モードのトグル + 出典バッジ | 小〜中 |
| `tests/test_refs.py` | 単体テスト | 中 |

Phase 1 完了後、Phase 2(整合性チェック)・Phase 3(改訂履歴)の基盤(条番号メタ・same-source 解決)がそのまま使える。
