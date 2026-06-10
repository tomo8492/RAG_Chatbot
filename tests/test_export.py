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


# ---------------- ファイル名(safe_stem) ----------------
def test_safe_stem_removes_forbidden_chars():
    assert export.safe_stem('a/b:c*?"<>|d') == "abcd"


def test_safe_stem_trims_trailing_dot_and_space():
    assert export.safe_stem("報告書. ") == "報告書"        # Windows: 末尾のドット/空白は不可
    assert not export.safe_stem("名前 ").endswith(" ")


def test_safe_stem_avoids_reserved_names():
    assert export.safe_stem("CON") == "回答"
    assert export.safe_stem("nul") == "回答"               # 大文字小文字を問わず


def test_safe_stem_falls_back_when_empty():
    assert export.safe_stem("///") == "回答"
    assert export.safe_stem("") == "回答"


def test_safe_stem_truncates_length():
    assert len(export.safe_stem("あ" * 100)) <= 40


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


# ---------------- 参考図(出典の文書内画像)の埋め込み ----------------
def _fig_payload():
    """乱数ノイズPNG(圧縮で縮まない実画像の代役)を base64 で参考図形式にする。"""
    import base64
    import io
    import random
    from PIL import Image
    raw = random.Random(7).randbytes(240 * 160 * 3)
    buf = io.BytesIO()
    Image.frombytes("RGB", (240, 160), raw).save(buf, format="PNG")
    return [{"data": base64.b64encode(buf.getvalue()).decode(), "caption": "手順書.xlsx シート:組立"}]


def _zip_has_media(data: bytes, prefix: str) -> bool:
    import io
    import zipfile
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return any(n.startswith(prefix) for n in z.namelist())


def test_html_embeds_figures_as_data_uri():
    html = export.to_html(MD, figures=_fig_payload()).decode("utf-8")
    assert "参考図(出典)" in html and "data:image/png;base64," in html
    assert "手順書.xlsx シート:組立" in html
    assert "参考図" not in export.to_html(MD).decode("utf-8")   # 図なしなら節も出ない


def test_docx_embeds_figures():
    data = export.to_docx(MD, figures=_fig_payload())
    assert data.startswith(b"PK") and _zip_has_media(data, "word/media/")


def test_xlsx_embeds_figures_sheet():
    data = export.to_xlsx(TABLE_MD, figures=_fig_payload())
    assert data.startswith(b"PK") and _zip_has_media(data, "xl/media/")


def test_pptx_embeds_figures_slide():
    data = export.to_pptx(MD, figures=_fig_payload())
    assert data.startswith(b"PK") and _zip_has_media(data, "ppt/media/")


def test_pdf_embeds_figures():
    assert export.to_pdf(MD, figures=_fig_payload()).startswith(b"%PDF")


def test_export_content_passes_figures_and_ignores_for_text():
    figs = _fig_payload()
    data, _, _ = export.export_content(MD, "html", title="回答", figures=figs)
    assert b"data:image/png;base64," in data
    data, _, _ = export.export_content(MD, "md", title="回答", figures=figs)   # mdは無視
    assert b"data:image" not in data


def test_decode_figures_ignores_garbage():
    from app.export._render import _decode_figures
    figs = _decode_figures([{"data": "!!!not-base64!!!"}, None, {"caption": "のみ"},
                            *_fig_payload()])
    assert len(figs) == 1 and figs[0][1] == "手順書.xlsx シート:組立"


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
