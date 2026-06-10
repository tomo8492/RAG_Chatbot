"""agent の文脈(会話履歴)圧縮ヘルパ。

メッセージのテキスト抽出・文字数計算と、設定部分を保持したまま履歴を1件の要約へ
畳み込む `compact_ctx`(要約関数は注入)。要約LLMが使えない場合の最終手段として、
機械的に必ず縮める `force_truncate_ctx` も提供する。純粋関数で副作用なし。
モデルを使う `compact_ctx_with_model` はクライアント生成と一体のため _impl 側に置く。
"""
from __future__ import annotations

import json

from .constants import CTX_CHAR_LIMIT
from ..logging_setup import get_logger

log = get_logger("agent.context")


__all__ = ["_text_of", "_role_of", "_ctx_chars", "_head_len",
           "compact_ctx", "force_truncate_ctx"]


def _text_of(m) -> str:
    if isinstance(m, dict):
        return str(m.get("content") or "")
    return str(getattr(m, "content", "") or "")


def _role_of(m) -> str:
    return str((m.get("role") if isinstance(m, dict) else getattr(m, "role", "")) or "")


def _ctx_chars(messages: list) -> int:
    """文脈の概算サイズ(文字)。tool_calls の引数(write_file の全文など)も
    実際にプロンプトへ入るため算入する(本文だけ数えると大きく見積もり漏れする)。"""
    total = 0
    for m in messages:
        total += len(_text_of(m))
        tcs = m.get("tool_calls") if isinstance(m, dict) else None
        if tcs:
            try:
                total += len(json.dumps(tcs, ensure_ascii=False))
            except Exception:
                log.debug("_ctx_chars: 例外を無視して継続", exc_info=True)
                total += 200
    return total


def _head_len(messages: list) -> int:
    """先頭の設定メッセージ(system+作業フォルダ+CLAUDE.md+ack)までの数。"""
    for i, m in enumerate(messages):
        if _role_of(m) == "assistant":
            return i + 1
    return min(len(messages), 1)


def compact_ctx(messages: list, summarizer, char_limit: int = CTX_CHAR_LIMIT,
                transcript_cap: int = 40000) -> bool:
    """文脈が大きければ、設定以降を要約1件に置き換える。置換したら True。
    summarizer(transcript:str)->str。構造を壊さないよう設定部分は保持する。
    char_limit は圧縮を起動する合計文字数のしきい値、transcript_cap は要約LLMへ渡す
    最大文字数(要約リクエスト自体が num_ctx を超えないよう、呼び出し側が窓に合わせて渡す)。"""
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
    transcript = "\n".join(lines)[-max(transcript_cap, 1000):]
    try:
        summary = (summarizer(transcript) or "").strip()
    except Exception:
        log.debug("compact_ctx: 例外を無視して継続", exc_info=True)
        return False
    if not summary:
        return False
    messages[head:] = [{"role": "user", "content": "【これまでの作業の要約(自動圧縮)】\n" + summary}]
    return True


# force_truncate_ctx の調整値
_TRUNC_TOOL_KEEP = 1500    # 切り詰め後に残すツール結果の文字数
_TRUNC_MSG_KEEP = 4000     # 〃 通常メッセージ(user/assistant)
_TRUNC_TAIL_KEEP = 6       # 間引き時も末尾に必ず残す件数(直近のやり取り)


def force_truncate_ctx(messages: list, char_limit: int) -> bool:
    """LLMを使わずに文脈を必ず char_limit 付近まで縮める最終手段。

    要約LLM自体がコンテキスト超過などで失敗したときの保険。
    ① 長いツール結果・本文を切り詰める → ② それでも超過なら古い履歴を頭から間引く。
    head(設定部)と直近 _TRUNC_TAIL_KEEP 件は維持する。戻り値: 変更したか。
    """
    changed = False
    head = _head_len(messages)

    def _cut(m, keep: int) -> None:
        nonlocal changed
        if not isinstance(m, dict):
            return
        txt = _text_of(m)
        if len(txt) > keep:
            m["content"] = txt[:keep] + f"\n…(文脈超過のため自動切り詰め: 元{len(txt)}文字)"
            changed = True

    for m in messages[head:]:
        _cut(m, _TRUNC_TOOL_KEEP if _role_of(m) == "tool" else _TRUNC_MSG_KEEP)

    dropped = 0
    while (_ctx_chars(messages) > char_limit
           and len(messages) - head > _TRUNC_TAIL_KEEP):
        messages.pop(head)
        dropped += 1
        changed = True
    if dropped:
        messages.insert(head, {"role": "user", "content":
                               f"【文脈超過のため、古い履歴 {dropped} 件を省略しました】"})
    return changed
