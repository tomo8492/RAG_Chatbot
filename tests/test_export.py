"""app.export(回答のファイル出力)の単体テスト。

各形式が壊れたバイト列でないこと(マジックナンバー)と、本文・表・図が
取り込まれていることを検証する。pytest でも
`python tests/test_export.py` 単体実行でも動く。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import export  # noqa: E402

MD = "# 見出し\n\n本文のテキスト。\n\n- 箇条書き1\n- 箇条書き2\n"
TABLE_MD = "| 名前 | 数量 |\n| --- | --- |\n| りんご | 3 |\n| みかん | 5 |\n"
MERMAID_MD = "# 図\n\n```mermaid\nflowchart LR\n  A --> B\n```\n"


# ---------------- マジックナンバー(壊れていないこと) ----------------
def test_pdf_magic():
    assert export.to_pdf(MD).startswith(b"%PDF")


def test_docx_is_zip():
    assert export.to_docx(MD).startswith(b"PK")          # OOXML = zip


def test_xlsx_is_zip():
    assert export.to_xlsx(TABLE_MD).startswith(b"PK")


def test_pptx_is_zip():
    assert export.to_pptx(MD).startswith(b"PK")


# ---------------- HTML ----------------
def test_html_contains_heading_text():
    html = export.to_html(MD).decode("utf-8")
    assert "<html" in html.lower() and "見出し" in html


def test_html_mermaid_embeds_library_and_block():
    html = export.to_html(MERMAID_MD).decode("utf-8")
    assert "mermaid" in html                              # ライブラリ参照
    assert "flowchart LR" in html                         # 図の定義が残る


# ---------------- CSV ----------------
def test_csv_contains_table_cells():
    csv = export.to_csv(TABLE_MD).decode("utf-8")
    assert "りんご" in csv and "みかん" in csv
    assert "名前" in csv and "数量" in csv


# ---------------- プレーンテキスト(.txt) ----------------
def test_txt_preserves_table_cells():
    s = export.export_content(TABLE_MD, "txt")[0].decode("utf-8")
    assert "りんご" in s and "みかん" in s and "名前" in s and "数量" in s   # 表が欠落しない


def test_txt_separates_blocks_with_blank_line():
    s = export.export_content("# 見出し\n\n本文だよ", "txt")[0].decode("utf-8")
    assert "見出し" in s and "本文だよ" in s
    assert "\n\n" in s                       # ブロック間に空行


def test_txt_list_keeps_number_and_nesting():
    s = export.export_content("1. 一\n2. 二\n   - ネスト", "txt")[0].decode("utf-8")
    assert "1. 一" in s and "2. 二" in s     # 番号を保持
    assert "    ・ネスト" in s                # 下位はインデント+箇条書き


def test_txt_task_checkbox_preserved():
    s = export.export_content("- [x] 完了\n- [ ] 未了", "txt")[0].decode("utf-8")
    assert "[x] 完了" in s and "[ ] 未了" in s


def test_txt_strips_emphasis_and_sets_mime():
    data, mime, ext = export.export_content("**太字** と `code`", "txt")
    s = data.decode("utf-8")
    assert "太字" in s and "code" in s and "**" not in s and "`" not in s
    assert ext == "txt" and mime.startswith("text/plain")


# ---------------- export_content のディスパッチ ----------------
def test_export_content_pdf():
    data, mime, ext = export.export_content(MD, "pdf")
    assert data.startswith(b"%PDF") and ext == "pdf" and "pdf" in mime


def test_export_content_md_passthrough():
    data, mime, ext = export.export_content("生のマークダウン", "md")
    assert data.decode("utf-8") == "生のマークダウン" and ext == "md"


def test_export_content_html():
    data, mime, ext = export.export_content(MD, "html")
    assert ext == "html" and b"<html" in data.lower()


def test_export_content_unknown_format_raises():
    try:
        export.export_content(MD, "doesnotexist")
    except ValueError:
        return
    raise AssertionError("未対応形式で例外が出なかった")


# ---------------- HTML変換の品質(バグ修正・忠実度) ----------------
def _h(md):
    return export.to_html(md).decode("utf-8")


def test_html_inline_code_is_literal():
    # コード内の ** は整形されずリテラルのまま
    assert "<code>a**b**c</code>" in _h("`a**b**c`")


def test_html_bolditalic_nesting_is_valid():
    h = _h("***強調***")
    assert "<strong><em>強調</em></strong>" in h
    assert "<strong><em>強調</strong></em>" not in h   # 壊れた閉じ順でない


def test_html_image_becomes_img_tag():
    assert '<img src="pic.png" alt="代替">' in _h("![代替](pic.png)")


def test_html_neutralizes_dangerous_url():
    h = _h("[x](javascript:alert(1))")
    assert "javascript:" not in h and 'href="#"' in h


def test_html_nested_list_structure():
    h = _h("- 親\n  - 子1\n  - 子2\n- 親2")
    assert "<li>親<ul><li>子1</li><li>子2</li></ul></li>" in h


def test_html_task_list_checkboxes():
    h = _h("- [ ] 未\n- [x] 済")
    assert '<input type="checkbox" disabled>' in h
    assert '<input type="checkbox" disabled checked>' in h


def test_html_heading_demoted_no_body_h1():
    body = _h("# 見出しX\n本文").split('<div class="doc-body">')[1]
    assert "<h2>見出しX</h2>" in body and "<h1" not in body


def test_html_underscore_emphasis_protects_snake_case():
    h = _h("__太__ と _斜_ と snake_case_x")
    assert "<strong>太</strong>" in h and "<em>斜</em>" in h
    assert "snake_case_x" in h and "<em>case</em>" not in h


def test_html_table_alignment_applied():
    h = _h("| 左 | 右 |\n|:--|--:|\n| a | b |")
    assert 'style="text-align:left"' in h and 'style="text-align:right"' in h


def test_html_code_highlight_inlined_only_when_code():
    with_code = _h("```python\nprint(1)\n```")
    assert "hljs.highlightAll" in with_code and 'class="language-python"' in with_code
    assert "hljs.highlightAll" not in _h("ただの段落です。")


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
