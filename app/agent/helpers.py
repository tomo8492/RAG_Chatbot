"""agent の入出力ヘルパ。

プロジェクト指示(CLAUDE.md 等)の読み込みと、ツール引数(todo/options/questions)の
正規化・ask 結果の整形。純粋関数中心(定数 PROJECT_FILES のみ依存)。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .constants import PROJECT_FILES
from ..logging_setup import get_logger

log = get_logger("agent.helpers")


__all__ = [
    "read_project_instructions",
    "_norm_todos", "_norm_options", "_norm_questions", "_norm_plan", "_format_ask_result",
]


def _norm_plan(raw) -> str:
    """present_plan の plan 引数を文字列に正規化する。

    手順を配列(["手順1", ...])やオブジェクトで渡すモデルがあり、
    そのまま .strip() するとクラッシュするため吸収する。"""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, (list, tuple)):
        items = [str(x).strip() for x in raw if str(x).strip()]
        return "\n".join(f"{i}. {s}" for i, s in enumerate(items, 1))
    if isinstance(raw, dict):
        try:
            return json.dumps(raw, ensure_ascii=False, indent=1)
        except Exception:
            log.debug("_norm_plan: 例外を無視して継続", exc_info=True)
            return str(raw)
    return str(raw or "").strip()


def read_project_instructions(ws: Path, limit: int = 8000,
                              sub_limit: int = 8, per_sub: int = 2000) -> Optional[str]:
    """作業フォルダ直下の CLAUDE.md / AGENTS.md に加え、直下サブフォルダの指示書も読む。

    モノレポやサブプロジェクトごとに指示書を分けているケースに対応する。
    - 直下: 最初に見つかった PROJECT_FILES を本体指示として全文(limit まで)
    - 直下サブフォルダ: 各フォルダの CLAUDE.md/AGENTS.md を per_sub 文字まで、最大 sub_limit 個
    どちらも見つからなければ None。
    """
    parts: list[str] = []
    # 1) 作業フォルダ直下の指示書(本体)
    for name in PROJECT_FILES:
        try:
            p = (ws / name)
            if p.is_file():
                text = p.read_text(encoding="utf-8", errors="replace").strip()
                if text:
                    parts.append(text[:limit] + ("\n...(省略)" if len(text) > limit else ""))
                    break
        except Exception:
            log.debug("read_project_instructions: 例外を無視して継続", exc_info=True)
            continue
    # 2) 直下サブフォルダの指示書(サブプロジェクト別)
    subs: list[str] = []
    try:
        for d in sorted(p for p in ws.iterdir() if p.is_dir()):
            if d.name.startswith(".") or d.name in ("node_modules", "__pycache__", "venv", ".venv"):
                continue
            for name in ("CLAUDE.md", "AGENTS.md"):
                sp = d / name
                if sp.is_file():
                    try:
                        txt = sp.read_text(encoding="utf-8", errors="replace").strip()
                    except Exception:
                        log.debug("read_project_instructions(sub): 例外を無視", exc_info=True)
                        txt = ""
                    if txt:
                        subs.append(f"### {d.name}/{name}\n"
                                    + txt[:per_sub] + ("\n...(省略)" if len(txt) > per_sub else ""))
                    break
            if len(subs) >= sub_limit:
                break
    except Exception:
        log.debug("read_project_instructions: サブフォルダ走査に失敗(無視)", exc_info=True)
    if subs:
        parts.append("【サブフォルダの指示書】\n" + "\n\n".join(subs))
    return "\n\n".join(parts) if parts else None


def _norm_todos(todos) -> list:
    """todo_write の引数を正規化(content/status のみ・状態を検証)。"""
    out = []
    if isinstance(todos, list):
        for t in todos:
            if not isinstance(t, dict):
                continue
            content = str(t.get("content") or t.get("task") or "").strip()
            status = str(t.get("status") or "pending").strip()
            if status not in ("pending", "in_progress", "completed"):
                status = "pending"
            if content:
                out.append({"content": content, "status": status})
    return out


def _norm_options(raw) -> list:
    """ask_user の選択肢を {label, description, recommended} に正規化。
    文字列・オブジェクトのどちらで来ても受け取れるようにする(弱いモデル対策)。"""
    if isinstance(raw, str):                      # 配列をJSON文字列で渡すモデルがある
        try:
            raw = json.loads(raw)
        except Exception:
            log.debug("_norm_options: 例外を無視して継続", exc_info=True)
            return []
    if isinstance(raw, dict):                     # 単一選択肢をオブジェクトのまま渡す場合
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    out, rec_used = [], False
    for o in raw:
        if isinstance(o, dict):
            label = str(o.get("label") or o.get("text") or o.get("value") or o.get("title") or "").strip()
            desc = str(o.get("description") or o.get("desc") or o.get("detail") or "").strip()
            rec = bool(o.get("recommended") or o.get("recommend") or o.get("default"))
        else:
            label, desc, rec = str(o).strip(), "", False
        if not label:
            continue
        if rec and rec_used:        # 推奨は最大1つに制限
            rec = False
        rec_used = rec_used or rec
        out.append({"label": label, "description": desc, "recommended": rec})
        if len(out) >= 4:
            break
    return out


def _norm_questions(args: dict) -> list:
    """ask_user の引数を質問リストに正規化する。
    新形式(questions 配列)・旧形式(question/options)・JSON文字列などのゆらぎを吸収。
    返り値: [{header, question, multiSelect, options:[{label,description,recommended}]}]"""
    raw = args.get("questions")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            log.debug("_norm_questions: 例外を無視して継続", exc_info=True)
            raw = None
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, (list, tuple)) or not raw:
        # 旧形式 / フォールバック(単一質問)
        raw = [{"header": args.get("header"), "question": args.get("question"),
                "multiSelect": args.get("multiSelect"), "options": args.get("options")}]
    out = []
    for q in raw:
        if not isinstance(q, dict):
            q = {"question": str(q)}
        question = str(q.get("question") or q.get("text") or q.get("title") or "").strip()
        header = str(q.get("header") or q.get("name") or "").strip()
        multi = bool(q.get("multiSelect") or q.get("multi") or q.get("multiple"))
        options = _norm_options(q.get("options"))
        if not question and not options:
            continue
        out.append({"header": header, "question": question or "どれにしますか?",
                    "multiSelect": multi, "options": options})
        if len(out) >= 3:
            break
    return out


def _format_ask_result(questions: list, ans) -> str:
    """ユーザーの回答(質問ごとの選択ラベル配列 / 文字列 / None)を、モデル向けの
    分かりやすいテキストに整形する。選んだ選択肢の説明も併記して取り違えを防ぐ。"""
    if isinstance(ans, list):
        per = ans
    elif isinstance(ans, str) and ans.strip():
        per = [[ans.strip()]]      # 旧形式(単一回答)
    else:
        per = []
    lines = ["ユーザーの回答:"]
    for i, q in enumerate(questions):
        sel = per[i] if i < len(per) else []
        if isinstance(sel, str):
            sel = [sel]
        sel = [str(s).strip() for s in (sel or []) if str(s).strip()]
        head = q.get("header") or q.get("question") or f"質問{i + 1}"
        if not sel:
            lines.append(f"- {head}: (回答なし)")
            continue
        parts = []
        for s in sel:
            o = next((o for o in q["options"] if o["label"] == s), None)
            parts.append(f"{s}({o['description']})" if o and o.get("description") else s)
        lines.append(f"- {head}: " + " / ".join(parts))
    return "\n".join(lines)


