"""
retrieval.py
密ベクトル検索の結果を、依存追加なしの語彙スコア(BM25相当)と
RRF(Reciprocal Rank Fusion)で再ランクし、重複除去・ソース多様化・
明らかに無関係なヒットの足切りを行う。

狙い:E5-small の密検索が苦手な「完全一致語(型番・条番号・固有名詞)」を
語彙シグナルで拾い直し、上位の並びを安定させて回答の根拠精度を上げる。

純粋関数のみで chromadb / 埋め込みモデルに依存しないため、単体テスト可能。
"""
from __future__ import annotations

import math
import re
from typing import Callable

# RRF の定数(大きいほど順位差の影響がなだらか。情報検索で一般的な 60)
RRF_K = 60
# 語彙スコアの融合重み(密検索を主、語彙を補助とする)
LEX_WEIGHT = 1.0
# 会話への添付(ユーザーが明示的に付けた資料)に与える優先度。KB と混在するとき、関連が
# 拮抗した添付が埋もれないよう少しだけ持ち上げる(無関係な添付までは引き上げない控えめな値)。
ATTACH_BONUS = 0.5
# これ未満の類似度(=1-cosine距離)は「ほぼ無関係」として落とす保守的な床
MIN_SCORE_FLOOR = 0.05
# 候補プールの上限(再ランクのO(n^2)処理を抑える)
MAX_CANDIDATES = 200

# ASCII 語(ABC-123, ver1.2 のような型番/識別子はまとめて1トークン)
_ASCII_TOKEN = re.compile(r"[a-z0-9]+(?:[._\-/][a-z0-9]+)*")
# CJK(かな・漢字)の連なり
_CJK_RUN = re.compile(r"[぀-ヿ㐀-鿿豈-﫿々〆ヵヶ]+")


def tokenize(text: str) -> list[str]:
    """言語非依存の軽量トークナイザ。

    - ASCII は語単位(型番 ABC-123 等は分割しない)
    - CJK は文字バイグラム(漢字熟語の部分一致に有効。共通語はIDFで自然に減衰)
    形態素解析器に依存しないため、オフラインでも追加導入なしで動く。
    """
    if not text:
        return []
    low = text.lower()
    tokens = _ASCII_TOKEN.findall(low)
    for run in _CJK_RUN.findall(low):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


def bm25_scores(query_tokens: list[str], docs_tokens: list[list[str]],
                k1: float = 1.5, b: float = 0.75) -> list[float]:
    """候補集合を corpus とみなした BM25 スコア(クエリと各文書の語彙的一致度)。"""
    n = len(docs_tokens)
    if n == 0 or not query_tokens:
        return [0.0] * n
    df: dict[str, int] = {}
    for toks in docs_tokens:
        for t in set(toks):
            df[t] = df.get(t, 0) + 1
    avgdl = sum(len(t) for t in docs_tokens) / n or 1.0
    q_unique = set(query_tokens)
    scores: list[float] = []
    for toks in docs_tokens:
        if not toks:
            scores.append(0.0)
            continue
        tf: dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        dl = len(toks)
        s = 0.0
        for t in q_unique:
            f = tf.get(t)
            if not f:
                continue
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            s += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / avgdl))
        scores.append(s)
    return scores


def _norm_text(t: str) -> str:
    return " ".join((t or "").split())


def _dedup(hits: list[dict]) -> list[dict]:
    """近重複チャンク(オーバーラップ由来の同文・包含)を、距離が小さい方を残して除去。"""
    kept: list[dict] = []
    kept_norm: list[str] = []
    for h in sorted(hits, key=lambda x: x.get("distance", 1.0)):
        nt = _norm_text(h.get("text", ""))
        if not nt:
            continue
        short = nt[:240]
        dup = False
        for kn in kept_norm:
            # 一方が他方を含む / 先頭240字が一致 ≒ 同一チャンク
            if short and (short in kn or kn[:240] in nt):
                dup = True
                break
        if dup:
            continue
        kept.append(h)
        kept_norm.append(nt)
    return kept


def rerank(query: str, hits: list[dict], top_k: int,
           max_per_source: int = 5, unlimited: bool = False) -> list[dict]:
    """密検索のヒット列を、語彙スコアとのRRF融合で再ランクして返す。

    手順: 足切り → 重複除去 → 密順位 + 語彙順位(BM25)を RRF 融合 →
    ソース単位の多様化 → top_k(unlimited は全件)。
    """
    if not hits:
        return []
    # 1) 明らかに無関係(類似度がほぼ0)を落とす保守的な足切り
    pool = [h for h in hits if h.get("score", 0.0) > MIN_SCORE_FLOOR] or list(hits)
    # 2) 近重複除去 + 候補数の上限
    pool = _dedup(pool)[:MAX_CANDIDATES]
    n = len(pool)
    if n == 1:
        return pool

    # 3) 密検索の順位(距離の昇順)
    dense_order = sorted(range(n), key=lambda i: pool[i].get("distance", 1.0))
    fused = [0.0] * n
    for rank, idx in enumerate(dense_order):
        fused[idx] += 1.0 / (RRF_K + rank + 1)

    # 4) 語彙スコア(BM25)の順位。クエリに使える語が無ければ密のみ。
    q_tokens = tokenize(query)
    if q_tokens:
        # Contextual BM25: 文脈付き埋め込みが有効なら、文書の文脈(ctx)も語彙照合に含める
        # (型番・固有名詞だけでなく「どの文書か」を示す語でも拾えるようにする)
        docs_tokens = [tokenize(((h.get("ctx") or "") + " " + (h.get("text") or "")).strip()) for h in pool]
        lex = bm25_scores(q_tokens, docs_tokens)
        lex_order = [i for i in sorted(range(n), key=lambda i: lex[i], reverse=True)
                     if lex[i] > 0.0]
        for rank, idx in enumerate(lex_order):
            fused[idx] += LEX_WEIGHT / (RRF_K + rank + 1)

    # 4.5) 会話への添付を控えめに優先(関連が拮抗したときに埋もれさせない。
    #      無関係な添付は元の密+語彙が低いので、このブースト程度では上位に来ない)
    for i in range(n):
        if pool[i].get("attachment"):
            fused[i] += ATTACH_BONUS / RRF_K

    # 5) 融合スコア降順(同点は密距離が近い順)
    order = sorted(range(n), key=lambda i: (-fused[i], pool[i].get("distance", 1.0)))

    # 6) ソース単位の多様化(1ファイルの独占を防ぐ)
    out: list[dict] = []
    per: dict[str, int] = {}
    for i in order:
        h = pool[i]
        src = h.get("source", "")
        if per.get(src, 0) >= max_per_source:
            continue
        per[src] = per.get(src, 0) + 1
        out.append(h)
    return out if unlimited else out[:top_k]


def llm_rerank(query: str, hits: list[dict], top_k: int,
               score_fn: Callable[[str, list[str]], list[float]],
               max_per_source: int = 5) -> list[dict]:
    """RRF融合済みの候補を、LLMの関連度スコア(score_fn 注入)で並べ替えて top_k 返す。

    score_fn(query, [text,...]) -> [score,...]。長さ不一致・失敗時は入力順を維持する
    (=融合順へフォールバック)。LLM/外部依存を持たない純関数なので単体テスト可能。
    """
    if not hits:
        return []
    texts = [h.get("text", "") for h in hits]
    try:
        scores = score_fn(query, texts)
    except Exception:
        scores = []
    if scores and len(scores) == len(hits):
        order = sorted(range(len(hits)), key=lambda i: (-float(scores[i]), i))
        ranked = [hits[i] for i in order]
    else:
        ranked = list(hits)   # フォールバック: 融合順を維持
    out: list[dict] = []
    per: dict[str, int] = {}
    for h in ranked:
        src = h.get("source", "")
        if per.get(src, 0) >= max_per_source:
            continue
        per[src] = per.get(src, 0) + 1
        out.append(h)
    return out[:top_k]
