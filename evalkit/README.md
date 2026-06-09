# evalkit — RAG評価セット(着手前ゲート②)

RAG改善の効果を体感頼みにしないための軽量な前後比較ツール。
**RAGパイプライン本体は変更しない**(read-only に `rag.retrieve` / `llm.chat_stream` を呼ぶだけ)。

## 使い方

1. 既存KBのインデックスIDを確認:
   ```
   python evalkit/run_eval.py --list-indexes
   ```

2. テンプレをコピーして自社の質問を10〜20問用意:
   ```
   cp evalkit/eval_set.example.json evalkit/eval_set.json
   # index_ids を実IDに、questions を実際の質問+expected_files に編集
   ```
   - `expected_files`: その質問の根拠が載っている**期待ファイル名**(複数可)
   - `expected_answer_contains`: 回答に**含まれてほしい語句**(任意・部分一致)
   - 失敗3分類(a誤検索 / b生成 / cOCR)を各1問以上入れると効果が見えやすい

3. ベースライン(改善前)を計測:
   ```
   python evalkit/run_eval.py --set evalkit/eval_set.json --tag before --generate
   ```

4. 1段ずつ改善を入れた後、同じセットで再計測 → 比較:
   ```
   python evalkit/run_eval.py --set evalkit/eval_set.json --tag after_ocr --generate
   python evalkit/run_eval.py --compare evalkit/results/before_*.json evalkit/results/after_ocr_*.json
   ```

## 改善別の測り方(①文脈付き埋め込み / ②リランク)
- **② リランク**(検索のクエリ時処理)は1コマンドで前後比較できる(`--rerank` はその実行だけ
  設定を上書きし、終了時に元へ戻す):
  ```
  python evalkit/run_eval.py --set evalkit/eval_set.json --tag rr_off --rerank off
  python evalkit/run_eval.py --set evalkit/eval_set.json --tag rr_on  --rerank on
  python evalkit/run_eval.py --compare evalkit/results/rr_off_*.json evalkit/results/rr_on_*.json
  ```
- **① 文脈付き埋め込み**(インデックス時処理)は**再構築が必要**:
  1. OFFで構築 → `--tag ctx_off` で計測
  2. 設定で「文脈付き埋め込み」をON → 該当インデックスを**再構築** → `--tag ctx_on` で計測
  3. `--compare` で before/after

## 記録される指標
- `file_hit`: 期待ファイルが検索ヒットに入ったか
- `first_rank`: 期待ファイルが最初に出た順位(小さいほど良い)
- `answer_match`: `expected_answer_contains` を全て含むか(`--generate` 時)
- サマリ: `file_hit_rate`(Recall@k) / `mean_first_rank` / **`mrr`(平均逆順位)** /
  **`hit_at_1` / `hit_at_3`**(上位N件に期待ファイルがある割合)/ `answer_match_rate`
  - **リランク(②)の効果は `mrr` / `hit_at_1` に出やすい**(上位の並びが良くなるため)

各段(①OCR→②チャンキング→③検索)を**1段ごとに** before/after で測り、ゲートを切る。

## 注意
- 実行には Ollama・埋め込みモデル・構築済みインデックスが必要(=実機で実行)。
- `--generate` はチャットと同じ `build_messages`(strict) + `chat_stream` を使うため、実パイプラインに忠実。
- `evalkit/results/` は出力先(gitignore 済み)。
