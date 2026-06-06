"""app.splitter(分割器)の単体テスト。特に split_structured(見出し対応)。

pytest でも `python tests/test_splitter.py` 単体実行でも動く。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.splitter import split_structured, split_text  # noqa: E402


def _paths(out):
    return {p for _, p in out}


# ---------------- split_text(従来) ----------------
def test_split_text_short_is_single():
    assert split_text("短い文。", 800, 120) == ["短い文。"]


def test_split_text_respects_size():
    chunks = split_text("あ" * 2000, 500, 50)
    # 長文は複数チャンクに分かれる(オーバーラップ引き継ぎで多少前後するため上限は緩め)
    assert len(chunks) > 1
    assert all(c for c in chunks) and all(len(c) <= 500 * 2 for c in chunks)


# ---------------- split_structured ----------------
def test_no_heading_returns_whole_with_empty_path():
    out = split_structured("ただの本文です。改行もあります。", 800, 120)
    assert len(out) == 1 and out[0][1] == ""


def test_markdown_nested_heading_path():
    out = split_structured("# 規程\n本文A\n## 給与\n本文B", 800, 120)
    assert ("本文A", "規程") in out
    assert ("本文B", "規程 > 給与") in out
    # 見出し行そのものは本文チャンクに混ざらない
    assert all(not c.startswith("#") for c, _ in out)


def test_japanese_article_heading_path():
    out = split_structured("第1章 総則\n目的を定める\n第3条 給与\n基本給を支給する", 800, 120)
    assert ("目的を定める", "第1章 総則") in out
    assert ("基本給を支給する", "第1章 総則 > 第3条 給与") in out


def test_sentence_is_not_treated_as_heading():
    out = split_structured("これは説明の文です。\n続きの段落です。", 800, 120)
    assert _paths(out) == {""}


def test_numbered_subheading_detected_but_plain_list_not():
    out = split_structured("1. りんごを買う\n2.1 詳細仕様\n本文X", 800, 120)
    # "1. ..." は見出し扱いしない(本文・パス空)/ "2.1 ..." は見出し
    assert any(p == "" and "りんご" in c for c, p in out)
    assert any("詳細仕様" in p and "本文X" in c for c, p in out)


def test_bracket_heading():
    out = split_structured("【概要】\nこの章の概要本文。", 800, 120)
    assert any(p == "【概要】" for _, p in out)


def test_chunking_under_heading_keeps_path():
    out = split_structured("# 長い節\n" + "あ" * 1800, 500, 50)
    assert len(out) > 1 and all(p == "長い節" for _, p in out)


def test_empty_text():
    assert split_structured("", 800, 120) == []


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
