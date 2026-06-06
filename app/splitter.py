"""
splitter.py
軽量な再帰的テキスト分割器。日本語の区切り(句点・読点)も考慮する。
langchain に依存しない自前実装。

split_structured は見出し階層(Markdown # / 第N条 / 章節 / 番号付き / 【…】 等)を
解釈し、各チャンクに「見出しパス」を付与する。これにより
  - 埋め込み・語彙検索に節の語が入りヒット率が上がる
  - 出典に「どの節か」を示せて根拠提示が正確になる
"""
from __future__ import annotations

import re

_SEPARATORS = ["\n\n", "\n", "。", "、", "．", "，", " ", ""]

# 見出し判定で使う正規表現(日本語の文書構造に対応)
_ATX = re.compile(r"^(#{1,6})\s+(.+)$")                       # Markdown 見出し
_JA_STRUCT = re.compile(r"^第[0-9０-９一二三四五六七八九十百千]+(編|章|節|款|条|項|号)")
_NUMBERED = re.compile(r"^([0-9]+(?:\.[0-9]+)+)[.\s]")        # 2.1 / 3.2.1 等(サブ番号付き)
_BRACKET = re.compile(r"^【.+】")                              # 【概要】等
_BULLET_HEAD = re.compile(r"^[■◆●▼▽◇○]\s*\S")               # ■見出し 等
# 章節の相対的な深さ(数値の大小だけが意味を持つ)
_JA_LEVEL = {"編": 1, "章": 2, "節": 3, "款": 4, "条": 4, "項": 5, "号": 5}
_MAX_TITLE = 50              # 見出しパスに使う各タイトルの最大長
_PATH_DEPTH = 3             # 見出しパスに含める階層数(末尾から)


def _split_by(text: str, sep: str) -> list[str]:
    if sep == "":
        return list(text)
    # セパレータを残して分割(文脈を保つため区切り文字を後ろに付ける)
    parts = text.split(sep)
    out = []
    for i, p in enumerate(parts):
        if i < len(parts) - 1:
            out.append(p + sep)
        else:
            out.append(p)
    return [p for p in out if p != ""]


def _merge(splits: list[str], chunk_size: int, overlap: int) -> list[str]:
    chunks: list[str] = []
    cur = ""
    for s in splits:
        if len(cur) + len(s) <= chunk_size or not cur:
            cur += s
        else:
            chunks.append(cur)
            # オーバーラップ分を引き継ぐ
            cur = (cur[-overlap:] if overlap > 0 else "") + s
    if cur.strip():
        chunks.append(cur)
    return chunks


def split_text(text: str, chunk_size: int = 800, overlap: int = 120,
               seps: list[str] | None = None) -> list[str]:
    """テキストを chunk_size 程度に分割する。"""
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    seps = seps if seps is not None else _SEPARATORS
    sep = seps[0]
    rest = seps[1:]

    pieces = _split_by(text, sep)

    final: list[str] = []
    for piece in pieces:
        if len(piece) <= chunk_size:
            final.append(piece)
        elif rest:
            # まだ細かく割れるセパレータが残っている
            final.extend(split_text(piece, chunk_size, overlap, rest))
        else:
            # これ以上割れない場合は強制カット
            for i in range(0, len(piece), chunk_size):
                final.append(piece[i:i + chunk_size])

    return _merge(final, chunk_size, overlap)


def _heading(line: str) -> tuple[int | None, str]:
    """行が見出しなら (レベル, タイトル) を、そうでなければ (None, "") を返す。"""
    s = line.strip()
    if not s or len(s) > 80:
        return (None, "")
    m = _ATX.match(s)
    if m:
        return (len(m.group(1)), m.group(2).strip()[:_MAX_TITLE])
    m = _JA_STRUCT.match(s)
    if m:
        return (_JA_LEVEL.get(m.group(1), 4), s[:_MAX_TITLE])
    # 以下は文末が句点で終わる「普通の文」を誤検出しないようにする
    if s[-1] in "。.!?！?":
        return (None, "")
    m = _NUMBERED.match(s)
    if m:
        return (m.group(1).count(".") + 1, s[:_MAX_TITLE])
    if _BRACKET.match(s):
        return (1, s[:_MAX_TITLE])
    if _BULLET_HEAD.match(s):
        return (2, s.lstrip("■◆●▼▽◇○ 　")[:_MAX_TITLE])
    return (None, "")


def split_structured(text: str, chunk_size: int = 800, overlap: int = 120
                     ) -> list[tuple[str, str]]:
    """見出し階層を解釈し、(チャンク本文, 見出しパス) のリストを返す。

    見出しパスは "第3章 給与 > 第12条 基本給" のような文字列(末尾 _PATH_DEPTH 階層)。
    見出しが全く無いテキストは ("", 全体) として通常分割する。
    """
    text = (text or "").strip()
    if not text:
        return []

    stack: list[tuple[int, str]] = []
    segments: list[tuple[str, str]] = []   # (見出しパス, 本文)
    cur_path = ""
    buf: list[str] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        if body:
            segments.append((cur_path, body))
        buf.clear()

    for line in text.split("\n"):
        lvl, title = _heading(line)
        if lvl is not None:
            flush()
            while stack and stack[-1][0] >= lvl:
                stack.pop()
            stack.append((lvl, title))
            cur_path = " > ".join(t for _, t in stack[-_PATH_DEPTH:])
        else:
            buf.append(line)
    flush()

    out: list[tuple[str, str]] = []
    for path, body in segments:
        for chunk in split_text(body, chunk_size, overlap):
            out.append((chunk, path))
    if not out:
        # 見出しのみで本文が無い等。索引から取りこぼさないよう全体を素直に分割する。
        return [(c, "") for c in split_text(text, chunk_size, overlap)]
    return out
