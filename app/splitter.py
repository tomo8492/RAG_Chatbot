"""
splitter.py
軽量な再帰的テキスト分割器。日本語の区切り(句点・読点)も考慮する。
langchain に依存しない自前実装。
"""
from __future__ import annotations

_SEPARATORS = ["\n\n", "\n", "。", "、", "．", "，", " ", ""]


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
