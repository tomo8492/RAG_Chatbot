"""export の共有層: Markdown ブロック解析とインライン整形。

各形式レンダラ(_render)が共通で使う純粋関数。re と html のみ依存。
"""

import html as _html
import re

def parse_blocks(md: str) -> list[dict]:
    lines = (md or "").replace("\r\n", "\n").split("\n")
    blocks: list[dict] = []
    i, n = 0, len(lines)

    def is_table_sep(s: str) -> bool:
        return bool(re.match(r"^\s*\|?[\s:\-\|]+\|?\s*$", s)) and "-" in s

    while i < n:
        line = lines[i]

        # コードフェンス
        m = re.match(r"^\s*```(.*)$", line)
        if m:
            lang = m.group(1).strip()
            i += 1
            buf = []
            while i < n and not re.match(r"^\s*```\s*$", lines[i]):
                buf.append(lines[i]); i += 1
            i += 1  # 終端 ``` をスキップ
            blocks.append({"type": "code", "lang": lang, "text": "\n".join(buf)})
            continue

        # 空行
        if line.strip() == "":
            i += 1
            continue

        # 見出し
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            blocks.append({"type": "heading", "level": len(m.group(1)), "text": m.group(2).strip()})
            i += 1
            continue

        # テーブル
        if "|" in line and i + 1 < n and is_table_sep(lines[i + 1]):
            header = _split_row(line)
            aligns = _col_aligns(lines[i + 1])
            i += 2
            rows = []
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(_split_row(lines[i])); i += 1
            blocks.append({"type": "table", "header": header, "rows": rows, "aligns": aligns})
            continue

        # 引用
        if re.match(r"^\s*>\s?", line):
            buf = []
            while i < n and re.match(r"^\s*>\s?", lines[i]):
                buf.append(re.sub(r"^\s*>\s?", "", lines[i])); i += 1
            blocks.append({"type": "quote", "text": "\n".join(buf)})
            continue

        # リスト(ネスト・タスクリスト対応)
        if re.match(r"^\s*([-*+]|\d+[.)])\s+", line):
            items = []
            while i < n and re.match(r"^(\s*)([-*+]|\d+[.)])\s+", lines[i]):
                mm = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", lines[i])
                indent = len(mm.group(1).expandtabs(4))
                ordered = bool(re.match(r"\d+[.)]", mm.group(2)))
                content = mm.group(3).strip()
                task = None
                tm = re.match(r"^\[([ xX])\]\s+(.*)$", content)
                if tm:
                    task = tm.group(1).lower() == "x"
                    content = tm.group(2)
                items.append({"indent": indent, "ordered": ordered,
                              "content": content, "task": task})
                i += 1
            blocks.append({"type": "list", "items": items})
            continue

        # 段落(連続する非空行をまとめる)
        buf = []
        while i < n and lines[i].strip() != "" and not re.match(r"^\s*```", lines[i]) \
                and not re.match(r"^(#{1,6})\s+", lines[i]) \
                and not re.match(r"^\s*([-*+]|\d+[.)])\s+", lines[i]) \
                and not re.match(r"^\s*>\s?", lines[i]):
            buf.append(lines[i]); i += 1
        blocks.append({"type": "paragraph", "text": "\n".join(buf)})

    return blocks


def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _col_aligns(sep_line: str) -> list[str]:
    """テーブル区切り行(:---: 等)から列ごとの text-align を求める。"""
    aligns = []
    for cell in _split_row(sep_line):
        c = cell.strip()
        left, right = c.startswith(":"), c.endswith(":")
        aligns.append("center" if left and right else
                      "right" if right else "left" if left else "")
    return aligns


def _align_style(aligns: list[str], j: int) -> str:
    a = aligns[j] if j < len(aligns) else ""
    return f' style="text-align:{a}"' if a else ""


def _render_list(items: list[dict]) -> str:
    """インデント深さから入れ子の <ul>/<ol> を構築する(タスクリスト対応)。"""
    out: list[str] = []
    stack: list[tuple[int, str]] = []   # (indent, tag)
    for it in items:
        ind = it["indent"]
        tag = "ol" if it["ordered"] else "ul"
        if not stack:
            out.append(f"<{tag}>")
            stack.append((ind, tag))
        elif ind > stack[-1][0]:
            out.append(f"<{tag}>")               # 直前の <li> の中に入れ子で開く
            stack.append((ind, tag))
        else:
            out.append("</li>")                   # 同階層: 直前の項目を閉じる
            while len(stack) > 1 and ind < stack[-1][0]:
                out.append(f"</{stack.pop()[1]}></li>")
        content = _inline_html(it["content"])
        if it["task"] is not None:
            checked = " checked" if it["task"] else ""
            content = (f'<input type="checkbox" disabled{checked}> ' + content)
        cls = ' class="task"' if it["task"] is not None else ""
        out.append(f"<li{cls}>{content}")
    while stack:
        out.append("</li>")
        out.append(f"</{stack.pop()[1]}>")
    return "".join(out)


def _item_plain(it: dict) -> str:
    """リスト項目の素テキスト(タスクマーカー付き)。docx/pdf/pptx/txt/csv 共通。"""
    t = it["content"]
    if it.get("task") is not None:
        t = ("[x] " if it["task"] else "[ ] ") + t
    return t


def _item_level(it: dict) -> int:
    """インデント(空白文字数)→ ネスト段数(0〜4)。"""
    return min(it.get("indent", 0) // 2, 4)


# ---- インライン処理 ----
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _strip_inline(text: str) -> str:
    """強調記号などを除いたプレーン文字列。"""
    text = _LINK.sub(r"\1 (\2)", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"(?<!\*)\*(?!\*)([^*]+)\*(?!\*)", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    return text


def _safe_url(url: str) -> str:
    """危険なスキーム(javascript: 等)を無効化したURLを返す。"""
    u = (url or "").strip()
    if re.match(r"^\s*(javascript|vbscript)\s*:", u, re.IGNORECASE):
        return "#"
    return u


def _inline_html(text: str) -> str:
    """インライン記法をHTMLへ。コード/リンク/画像を先に退避してから整形し、
    コード内が整形されない・タグのネストが壊れない・URLが安全になるようにする。"""
    holes: list[str] = []

    def stash(html_fragment: str) -> str:
        holes.append(html_fragment)
        return f"\x00H{len(holes) - 1}\x00"

    # 1) インラインコード(中身はリテラル。他の整形を一切受けない)
    s = re.sub(r"`([^`]+)`", lambda m: stash(f"<code>{_html.escape(m.group(1))}</code>"), text)
    # 2) 画像 ![alt](url)(リンクより先に処理)
    s = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)",
               lambda m: stash(f'<img src="{_html.escape(_safe_url(m.group(2)))}" '
                               f'alt="{_html.escape(m.group(1))}">'), s)
    # 3) リンク [text](url)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)",
               lambda m: stash(f'<a href="{_html.escape(_safe_url(m.group(2)))}">'
                               f"{_html.escape(m.group(1))}</a>"), s)
    # 4) 残りをエスケープ
    out = _html.escape(s)
    # 5) 強調(*** を最初に。閉じ順を正しく出す)
    out = re.sub(r"\*\*\*(.+?)\*\*\*", r"<strong><em>\1</em></strong>", out)
    out = re.sub(r"___(.+?)___", r"<strong><em>\1</em></strong>", out)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<!\w)__(?!_)(.+?)__(?!\w)", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<!\*)\*(?!\*)([^*]+)\*(?!\*)", r"<em>\1</em>", out)
    out = re.sub(r"(?<!\w)_(?!_)([^_]+)_(?!\w)", r"<em>\1</em>", out)
    # 6) 退避したHTMLを復元
    out = re.sub(r"\x00H(\d+)\x00", lambda m: holes[int(m.group(1))], out)
    return out


def _inline_runs(text: str) -> list[tuple[str, dict]]:
    """docx 用: (テキスト, {bold,italic,code}) のリスト。"""
    text = _LINK.sub(r"\1 (\2)", text)
    parts: list[tuple[str, dict]] = []
    pattern = re.compile(r"(\*\*.+?\*\*|`[^`]+`|(?<!\*)\*(?!\*)[^*]+\*(?!\*))")
    pos = 0
    for m in pattern.finditer(text):
        if m.start() > pos:
            parts.append((text[pos:m.start()], {}))
        tok = m.group(0)
        if tok.startswith("**"):
            parts.append((tok[2:-2], {"bold": True}))
        elif tok.startswith("`"):
            parts.append((tok[1:-1], {"code": True}))
        else:
            parts.append((tok[1:-1], {"italic": True}))
        pos = m.end()
    if pos < len(text):
        parts.append((text[pos:], {}))
    return parts or [(text, {})]



__all__ = [
    "parse_blocks", "_split_row", "_col_aligns", "_align_style", "_render_list",
    "_item_plain", "_item_level", "_strip_inline", "_safe_url", "_inline_html", "_inline_runs",
]
