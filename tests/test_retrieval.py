"""app.retrieval(ハイブリッド再ランク)の単体テスト。

chromadb / 埋め込みモデルに依存しない純粋関数のみを検証する。
pytest でも `python tests/test_retrieval.py` 単体実行でも動く。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import retrieval  # noqa: E402


def _hit(text, source="a", distance=0.5, score=None):
    return {"text": text, "source": source, "loc": "",
            "distance": distance, "score": (1.0 - distance) if score is None else score}


# ---------------- tokenize ----------------
def test_tokenize_keeps_ascii_identifier():
    toks = retrieval.tokenize("型番 ABC-123 の ver1.2")
    assert "abc-123" in toks and "ver1.2" in toks


def test_tokenize_cjk_bigrams():
    toks = retrieval.tokenize("仕様書")
    assert "仕様" in toks and "様書" in toks


def test_tokenize_empty():
    assert retrieval.tokenize("") == []


# ---------------- bm25 ----------------
def test_bm25_prefers_matching_doc():
    q = retrieval.tokenize("ABC-123")
    docs = [retrieval.tokenize("型番 ABC-123 の仕様"), retrieval.tokenize("無関係な天気の話")]
    s = retrieval.bm25_scores(q, docs)
    assert s[0] > 0 and s[1] == 0.0


def test_bm25_empty_query():
    assert retrieval.bm25_scores([], [["a"], ["b"]]) == [0.0, 0.0]


# ---------------- rerank: 語彙融合で完全一致語を引き上げ ----------------
def test_rerank_lexical_lifts_exact_term():
    # 密検索では一般説明(距離0.20)が上、型番一致(距離0.35)が下。
    hits = [
        _hit("就業規則の一般的な説明文です。福利厚生について。", source="a", distance=0.20),
        _hit("型番 ABC-123 の仕様は最大出力100Wです。", source="b", distance=0.35),
    ]
    out = retrieval.rerank("ABC-123 の仕様", hits, top_k=2)
    assert out[0]["source"] == "b"        # 語彙一致で上位へ


def test_rerank_no_query_tokens_keeps_dense_order():
    hits = [_hit("文書一", source="a", distance=0.4), _hit("文書二", source="b", distance=0.2)]
    out = retrieval.rerank("", hits, top_k=2)
    assert out[0]["source"] == "b"        # 距離が近い方が上


# ---------------- rerank: 重複除去 ----------------
def test_rerank_dedup_keeps_closest():
    hits = [
        _hit("まったく同じチャンク本文がここにあります。", source="a", distance=0.5),
        _hit("まったく同じチャンク本文がここにあります。", source="a", distance=0.3),
    ]
    out = retrieval.rerank("チャンク", hits, top_k=5)
    assert len(out) == 1 and out[0]["distance"] == 0.3


# ---------------- rerank: ソース多様化 ----------------
def test_rerank_caps_per_source():
    hits = [_hit(f"ソースaの異なる本文その{i}番です。", source="a", distance=0.1 + i * 0.01)
            for i in range(7)]
    out = retrieval.rerank("本文", hits, top_k=10, max_per_source=5)
    assert len(out) == 5


# ---------------- rerank: 無関係ヒットの足切り ----------------
def test_rerank_drops_irrelevant_low_score():
    hits = [
        _hit("関連する内容の本文です。", source="a", distance=0.2),   # score 0.8
        _hit("全く無関係な内容。", source="b", distance=0.98, score=0.02),  # 足切り対象
    ]
    out = retrieval.rerank("関連", hits, top_k=5)
    assert all(h["source"] != "b" for h in out)


def test_rerank_floor_not_applied_when_all_low():
    # 全部が低スコアなら、空にせず元を返す(回答機会を奪わない)
    hits = [_hit("x", source="a", distance=0.99, score=0.01),
            _hit("y", source="b", distance=0.99, score=0.01)]
    out = retrieval.rerank("z", hits, top_k=5)
    assert len(out) >= 1


# ---------------- rerank: 端ケース ----------------
def test_rerank_empty():
    assert retrieval.rerank("q", [], top_k=5) == []


def test_rerank_unlimited_returns_all_diverse():
    hits = [_hit("内容1", source="a", distance=0.2),
            _hit("内容2", source="b", distance=0.3),
            _hit("内容3", source="c", distance=0.4)]
    out = retrieval.rerank("内容", hits, top_k=1, unlimited=True)
    assert len(out) == 3        # unlimited は top_k で切らない


# ---------------- Contextual BM25(文脈を語彙検索にも効かせる)----------------
def test_contextual_bm25_uses_ctx_to_rerank():
    """文脈付き埋め込み有効時、文書文脈(ctx)の語彙一致で密検索の劣後候補を引き上げる。"""
    hits = [
        {"text": "アルファ本文", "source": "a", "loc": "", "distance": 0.6, "score": 0.4,
         "ctx": "初品検査結果通知書"},
        {"text": "ベータ本文", "source": "b", "loc": "", "distance": 0.5, "score": 0.5,
         "ctx": ""},
    ]
    # クエリ語は a の ctx にだけ含まれる。密検索だけなら b(距離小)が上位だが、
    # Contextual BM25 で a が逆転して1位になる。
    out = retrieval.rerank("初品検査結果", hits, top_k=2)
    assert out[0]["source"] == "a"


def test_rerank_without_ctx_is_backward_compatible():
    """ctx キーが無い(従来)場合も本文のみで BM25 照合して動作する。"""
    hits = [_hit("初品検査結果の記録", source="a", distance=0.6),
            _hit("無関係な内容", source="b", distance=0.5)]
    out = retrieval.rerank("初品検査結果", hits, top_k=2)
    assert out[0]["source"] == "a"   # 本文一致で a が上位(ctx 無しでも動く)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            passed += 1
        except AssertionError as e:
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"ERROR {fn.__name__}: {e!r}")
    print(f"{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
