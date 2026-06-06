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
