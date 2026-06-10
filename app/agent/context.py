"""agent の文脈(会話履歴)圧縮ヘルパ。

メッセージのテキスト抽出・文字数計算と、設定部分を保持したまま履歴を1件の要約へ
畳み込む `compact_ctx`(要約関数は注入)。純粋関数で副作用なし(constants のみ依存)。
モデルを使う `compact_ctx_with_model` はクライアント生成と一体のため _impl 側に置く。
"""
from __future__ import annotations

from .constants import CTX_CHAR_LIMIT
from ..logging_setup import get_logger

log = get_logger("agent.context")


__all__ = ["_text_of", "_role_of", "_ctx_chars", "_head_len", "compact_ctx"]


def _text_of(m) -> str:
    if isinstance(m, dict):
        return str(m.get("content") or "")
    return str(getattr(m, "content", "") or "")


def _role_of(m) -> str:
    return str((m.get("role") if isinstance(m, dict) else getattr(m, "role", "")) or "")


def _ctx_chars(messages: list) -> int:
    return sum(len(_text_of(m)) for m in messages)


def _head_len(messages: list) -> int:
    """先頭の設定メッセージ(system+作業フォルダ+CLAUDE.md+ack)までの数。"""
    for i, m in enumerate(messages):
        if _role_of(m) == "assistant":
            return i + 1
    return min(len(messages), 1)


def compact_ctx(messages: list, summarizer, char_limit: int = CTX_CHAR_LIMIT) -> bool:
    """文脈が大きければ、設定以降を要約1件に置き換える。置換したら True。
    summarizer(transcript:str)->str。構造を壊さないよう設定部分は保持する。
    char_limit は圧縮を起動する合計文字数のしきい値(呼び出し側が num_ctx から算出して渡す)。"""
    if _ctx_chars(messages) <= char_limit:
        return False
    head = _head_len(messages)
    rest = messages[head:]
    if len(rest) <= 4:
        return False
    lines = []
    for m in rest:
        txt = _text_of(m)
        if txt:
            lines.append(f"{_role_of(m)}: {txt}")
    transcript = "\n".join(lines)[-40000:]
    try:
        summary = (summarizer(transcript) or "").strip()
    except Exception:
        log.debug("compact_ctx: 例外を無視して継続", exc_info=True)
        return False
    if not summary:
        return False
    messages[head:] = [{"role": "user", "content": "【これまでの作業の要約(自動圧縮)】\n" + summary}]
    return True
