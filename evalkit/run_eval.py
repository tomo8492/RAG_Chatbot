#!/usr/bin/env python3
"""RAG評価ランナー(着手前ゲート②)。

評価セットを流し、各質問の「ヒット元(source/loc/score)」と任意で「回答」を記録する。
RAGパイプライン本体は変更しない(read-only)。各改善の前後で同じセットを流し before/after を比較する。

使い方:
  # 既存KBのインデックスIDを確認
  python evalkit/run_eval.py --list-indexes

  # ベースライン計測(検索のみ)
  python evalkit/run_eval.py --set evalkit/eval_set.json --tag before

  # 回答も生成して記録
  python evalkit/run_eval.py --set evalkit/eval_set.json --tag before --generate

  # 改善後に同じセットを流して比較
  python evalkit/run_eval.py --set evalkit/eval_set.json --tag after --generate
  python evalkit/run_eval.py --compare evalkit/results/before_*.json evalkit/results/after_*.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from evalkit import metrics  # noqa: E402

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def cmd_list(_args) -> int:
    from app import db
    rows = db.list_indexes()
    if not rows:
        print("インデックスがありません。先にUIで参照資料フォルダを追加してください。")
        return 0
    print("ID\tNAME\tFILES\tCHUNKS\tSTATUS")
    for ix in rows:
        print(f"{ix.get('id')}\t{ix.get('name')}\t{ix.get('file_count')}\t{ix.get('chunk_count')}\t{ix.get('status')}")
    return 0


def cmd_run(args) -> int:
    from app import llm, postprocess, rag
    cfg = _load(args.set)
    index_ids = cfg.get("index_ids") or []
    top_k = int(cfg.get("top_k", 8))
    if not index_ids or any("REPLACE" in str(i) for i in index_ids):
        print("eval_set の index_ids を実在のインデックスIDに設定してください "
              "(`--list-indexes` で確認)。", file=sys.stderr)
        return 2
    model = args.model or cfg.get("model")
    if args.generate and not model:
        try:
            from app.defaults import get_defaults
            model = get_defaults().get("model")
        except Exception:
            pass
    if args.generate and not model:
        print("--generate にはモデルが必要です(--model か eval_set.model)。", file=sys.stderr)
        return 2

    # ② リランクの効果測定: この実行だけ rerank_enabled を強制(終了時に必ず復元)
    rr_prev = None
    if args.rerank:
        from app.defaults import get_defaults as _gd, set_defaults as _sd
        rr_prev = bool(_gd().get("rerank_enabled"))
        _sd({"rerank_enabled": args.rerank == "on"})
        print(f"[config] rerank_enabled = {args.rerank == 'on'}(この実行のみ)")
    try:
        rows: list[dict] = []
        for q in cfg.get("questions", []):
            question = q["question"]
            expected = q.get("expected_files", [])
            hits = rag.retrieve(question, index_ids, top_k=top_k)
            sources = [h.get("source", "") for h in hits]
            row = {
                "id": q.get("id"),
                "question": question,
                "expected_files": expected,
                "hits": [{"source": h.get("source"), "loc": h.get("loc"),
                          "score": round(float(h.get("score", 0)), 3)} for h in hits],
                "file_hit": metrics.file_hit(expected, sources),
                "first_rank": metrics.first_hit_rank(expected, sources),
            }
            if args.generate:
                msgs = llm.build_messages("", [{"role": "user", "content": question}], hits, strict=True)
                ans = "".join(ev.get("text", "") for ev in llm.chat_stream(msgs, model, num_predict=1024)
                              if ev.get("type") == "content")
                ans = postprocess.clean(ans)
                row["answer"] = ans
                needles = q.get("expected_answer_contains")
                row["answer_match"] = metrics.answer_contains(ans, needles) if needles else None
            rows.append(row)
            print(f"[{'OK ' if row['file_hit'] else 'MISS'}] {q.get('id', '?')}: "
                  f"rank={row['first_rank']}  {question[:42]}")

        summary = metrics.summarize(rows)
        os.makedirs(RESULTS_DIR, exist_ok=True)
        tag = args.tag or "run"
        path = os.path.join(RESULTS_DIR, f"{tag}_{time.strftime('%Y%m%d-%H%M%S')}.json")
        config = {"rerank_enabled": (args.rerank == "on") if args.rerank else None}
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"tag": tag, "top_k": top_k, "config": config,
                       "summary": summary, "rows": rows}, f, ensure_ascii=False, indent=2)
        print("\n=== サマリ ===")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print("保存:", path)
        return 0
    finally:
        if rr_prev is not None:
            from app.defaults import set_defaults as _sd
            _sd({"rerank_enabled": rr_prev})


def cmd_compare(args) -> int:
    before, after = _load(args.compare[0]), _load(args.compare[1])
    brows = {(r.get("id") or r["question"]): r for r in before["rows"]}
    arows = {(r.get("id") or r["question"]): r for r in after["rows"]}
    print(f"# 比較: before(tag={before.get('tag')}) → after(tag={after.get('tag')})\n")
    print("| id | file_hit | first_rank | answer_match |")
    print("|---|---|---|---|")
    for k, a in arows.items():
        b = brows.get(k, {})
        fh = f"{int(bool(b.get('file_hit')))}→{int(bool(a.get('file_hit')))}"
        rk = f"{b.get('first_rank')}→{a.get('first_rank')}"
        am = "" if a.get("answer_match") is None else f"{b.get('answer_match')}→{a.get('answer_match')}"
        print(f"| {k} | {fh} | {rk} | {am} |")
    print("\n## summary")
    print("before:", json.dumps(before.get("summary"), ensure_ascii=False))
    print("after :", json.dumps(after.get("summary"), ensure_ascii=False))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="RAG評価ランナー(ゲート②)")
    ap.add_argument("--set", help="評価セットJSON")
    ap.add_argument("--tag", help="結果タグ(before/after など)")
    ap.add_argument("--model", help="生成モデル(--generate時)")
    ap.add_argument("--generate", action="store_true", help="回答も生成して記録")
    ap.add_argument("--rerank", choices=["on", "off"],
                    help="この実行だけリランクを強制ON/OFF(②の効果測定。終了時に元へ復元)")
    ap.add_argument("--list-indexes", action="store_true", help="既存インデックス一覧を表示")
    ap.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"), help="2つの結果JSONを比較")
    args = ap.parse_args()
    if args.list_indexes:
        return cmd_list(args)
    if args.compare:
        return cmd_compare(args)
    if not args.set:
        ap.error("--set / --compare / --list-indexes のいずれかを指定してください")
    return cmd_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
