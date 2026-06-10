"""agent の承認/回答ステートマシン(human-in-the-loop)。

エージェントの変更・重要操作・質問(ask_user)に対する UI からの承認/拒否/回答を
待ち受ける。プロセス内の共有辞書 `_pending` を Lock で保護する(単一プロセス前提)。
依存は constants のみ。
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Optional

from .constants import CONFIRM_TIMEOUT

__all__ = [
    "new_pending", "resolve", "resolve_answer",
    "wait_answer", "wait", "wait_decision",
]


_pending: dict[str, dict] = {}
_pending_lock = threading.Lock()
# 待ち手(wait*)はタイムアウトで自分のエントリを pop するが、確認カードの yield 中に
# クライアントが切断すると wait* に到達せずエントリが残る。この経過時間を超えた
# エントリは次の new_pending 時に掃除する(取りこぼしリークの防止)。
_STALE_AFTER = CONFIRM_TIMEOUT * 2


def new_pending() -> str:
    aid = uuid.uuid4().hex
    now = time.monotonic()
    with _pending_lock:
        stale = [k for k, v in _pending.items()
                 if now - v.get("created", now) > _STALE_AFTER]
        for k in stale:
            _pending.pop(k, None)
        _pending[aid] = {"event": threading.Event(), "approved": False,
                         "answer": None, "scope": None, "reason": None, "created": now}
    return aid


def resolve(action_id: str, approved: bool, scope: Optional[str] = None,
            reason: Optional[str] = None) -> bool:
    """承認/拒否を記録。scope='always' なら以後このセッションの編集を自動適用する。
    reason は拒否理由(任意)で、モデルに「どう直すか」を伝えるために使う。"""
    with _pending_lock:
        p = _pending.get(action_id)
    if not p:
        return False
    p["approved"] = bool(approved)
    p["scope"] = scope
    p["reason"] = reason
    p["event"].set()
    return True


def resolve_answer(action_id: str, answer: "str | list") -> bool:
    """ask_user への回答(自由記述=str / 質問ごとの選択=list)を記録して待機側を起こす。"""
    with _pending_lock:
        p = _pending.get(action_id)
    if not p:
        return False
    p["answer"] = answer
    p["event"].set()
    return True


def wait_answer(action_id: str, timeout: float = CONFIRM_TIMEOUT) -> Optional[str]:
    """ask_user の回答待ち。回答文字列 / None(タイムアウト)。"""
    with _pending_lock:
        p = _pending.get(action_id)
    if not p:
        return None
    ok = p["event"].wait(timeout)
    with _pending_lock:
        p = _pending.pop(action_id, None)
    if not ok or p is None:
        return None
    return p.get("answer")


def wait(action_id: str, timeout: float = CONFIRM_TIMEOUT) -> Optional[bool]:
    """承認待ち。True=承認 / False=拒否 / None=タイムアウト。"""
    with _pending_lock:
        p = _pending.get(action_id)
    if not p:
        return None
    ok = p["event"].wait(timeout)
    with _pending_lock:
        p = _pending.pop(action_id, None)
    if not ok or p is None:
        return None
    return p["approved"]


def wait_decision(action_id: str, timeout: float = CONFIRM_TIMEOUT):
    """承認待ち。(approved, scope, reason) を返す。approved: True/False/None、
    scope: 'always' なら以後自動適用、reason: 拒否理由(任意)。"""
    with _pending_lock:
        p = _pending.get(action_id)
    if not p:
        return (None, None, None)
    ok = p["event"].wait(timeout)
    with _pending_lock:
        p = _pending.pop(action_id, None)
    if not ok or p is None:
        return (None, None, None)
    return (p["approved"], p.get("scope"), p.get("reason"))
