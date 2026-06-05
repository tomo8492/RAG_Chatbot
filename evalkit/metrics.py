"""評価指標(純粋関数・標準ライブラリのみ)。RAG改善の前後比較に使う。

run_eval から使う。RAGパイプライン本体には依存しないので単体テストできる。
"""
from __future__ import annotations

import os


def _norm(name: str) -> str:
    """ファイル名を比較用に正規化(ディレクトリ除去・小文字化・空白除去)。"""
    return os.path.basename(str(name or "")).strip().lower()


def file_hit(expected_files, hit_sources) -> bool:
    """期待ファイルのいずれかが、検索ヒットの source に含まれるか。"""
    exp = {_norm(e) for e in (expected_files or [])}
    got = {_norm(s) for s in (hit_sources or [])}
    return bool(exp & got)


def first_hit_rank(expected_files, hit_sources):
    """期待ファイルが最初に現れる順位(1始まり)。無ければ None。"""
    exp = {_norm(e) for e in (expected_files or [])}
    for i, s in enumerate(hit_sources or [], 1):
        if _norm(s) in exp:
            return i
    return None


def answer_contains(answer: str, needles) -> bool:
    """期待語句が「すべて」回答に含まれるか(部分一致・大文字小文字無視)。"""
    a = (answer or "").lower()
    return all((n or "").lower() in a for n in (needles or []))


def summarize(rows: list[dict]) -> dict:
    """各質問の結果行から集計を出す。

    row: {"file_hit": bool, "first_rank": int|None, "answer_match": bool|None}
    """
    n = len(rows)
    if n == 0:
        return {"questions": 0, "file_hit_rate": None, "mean_first_rank": None, "answer_match_rate": None}
    hits = sum(1 for r in rows if r.get("file_hit"))
    ranks = [r["first_rank"] for r in rows if r.get("first_rank")]
    ans = [r for r in rows if r.get("answer_match") is not None]
    ans_ok = sum(1 for r in ans if r.get("answer_match"))
    return {
        "questions": n,
        "file_hit_rate": round(hits / n, 3),
        "mean_first_rank": round(sum(ranks) / len(ranks), 2) if ranks else None,
        "answer_match_rate": round(ans_ok / len(ans), 3) if ans else None,
    }
