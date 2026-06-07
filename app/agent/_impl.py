"""
agent.py
Web版コーディングエージェント(Claude Code 風)。

指定された「作業フォルダ」の中だけで、ローカルLLM(Ollama)が
  - 調査(読み取り): list_files / read_file / glob / grep
  - 変更: write_file(全文) / edit_file(部分置換) / run_command(コマンド)
  - present_plan: 実行計画を提示して承認を得る(計画モード)
を行い、依頼を達成する。

計画モード(既定)では「調査 → 計画提示 → 承認 → 実行」の順で進む。
承認後の実行フェーズでは、ファイル編集は自動適用、run_command など重要操作は
そのつど承認を取る。計画モードを切ると、従来どおり「変更を許可」+毎回承認で動く。
すべてのファイル操作は作業フォルダ内に限定される。
"""
from __future__ import annotations

import difflib
import json
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Iterator, Optional

import ollama

from ..config import settings
from ..logging_setup import get_logger
from .constants import *  # 定数は constants へ集約(暫定star。submodule化で解消)
from .tools import dispatch, _safe_path

log = get_logger("agent")


def read_project_instructions(ws: Path, limit: int = 8000) -> Optional[str]:
    """作業フォルダの CLAUDE.md / AGENTS.md を読み、エージェントへの指示として返す。"""
    for name in PROJECT_FILES:
        try:
            p = (ws / name)
            if p.is_file():
                text = p.read_text(encoding="utf-8", errors="replace").strip()
                if text:
                    return text[:limit] + ("\n...(省略)" if len(text) > limit else "")
        except Exception:
            continue
    return None


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


# ============================================================
#  ツール実装(すべて作業フォルダ内に限定)
# ============================================================
# ============================================================
_pending: dict[str, dict] = {}
_pending_lock = threading.Lock()


def new_pending() -> str:
    aid = uuid.uuid4().hex
    with _pending_lock:
        _pending[aid] = {"event": threading.Event(), "approved": False,
                         "answer": None, "scope": None, "reason": None}
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


def resolve_answer(action_id: str, answer: str) -> bool:
    """ask_user への回答(自由記述/選択肢)を記録して待機側を起こす。"""
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


# ============================================================
#  表示用の整形
# ============================================================
def _preview_args(name: str, args: dict) -> dict:
    if name == "write_file":
        return {"path": args.get("path", ""), "length": len(args.get("content", "") or "")}
    if name == "edit_file":
        return {"path": args.get("path", "")}
    if name in ("run_command", "run_background"):
        return {"command": args.get("command", "")}
    if name in ("command_output", "stop_command"):
        return {"job_id": args.get("job_id", "")}
    if name == "read_file":
        return {"path": args.get("path", "")}
    if name in ("glob", "grep"):
        return {"pattern": args.get("pattern", "")}
    if name == "summarize_path":
        return {"path": args.get("path", "")}
    return {}


def _change_preview(ws: Path, name: str, args: dict) -> dict:
    """write_file / edit_file の変更後を予測し、差分(unified diff)を作る。"""
    path = args.get("path", "")
    old = ""
    exists = False
    try:
        p = _safe_path(ws, path)
        if p.exists() and p.is_file():
            exists = True
            old = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if name == "write_file":
        new = args.get("content", "") or ""
    else:  # edit_file
        old_s = args.get("old_string", "") or ""
        new_s = args.get("new_string", "") or ""
        if old_s and old_s in old:
            new = old.replace(old_s, new_s) if args.get("replace_all") else old.replace(old_s, new_s, 1)
        else:
            new = old  # 一致なし(実行時にエラーになる)
    diff = "\n".join(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile=(path + " (現在)") if exists else "(新規)",
        tofile=path + " (変更後)", lineterm="",
    ))
    diff = diff[:8000] + ("\n...(差分省略)" if len(diff) > 8000 else "")
    return {"path": path, "exists": exists, "diff": diff}


def _action_detail(ws: Path, name: str, args: dict) -> dict:
    if name in ("run_command", "run_background"):
        return {"command": args.get("command", "")}
    if name in ("write_file", "edit_file"):
        return _change_preview(ws, name, args)
    return {}


# ============================================================
#  コンテキスト自動圧縮(長くなったら古い履歴を要約に置換)
# ============================================================
CTX_CHAR_LIMIT = 60000   # 文脈の合計文字数がこれを超えたら圧縮

# コーディング向け生成パラメータ(安定性重視・ツール呼び出し優先で think は付けない)
AGENT_TEMPERATURE = 0.2
AGENT_TOP_P = 0.9
AGENT_NUM_PREDICT = 8192   # 1ステップの最大出力。長い編集/差分が途中で切れないよう大きめ


def _gen_options(num_ctx: Optional[int] = None) -> dict:
    """エージェントの生成オプション。サンプリングはコーディング向けに固定し、
    num_ctx(コンテキスト長)だけは会話設定の値を反映する(0/None はモデル既定)。"""
    opt = {
        "temperature": AGENT_TEMPERATURE,
        "top_p": AGENT_TOP_P,
        "num_predict": AGENT_NUM_PREDICT,
    }
    if num_ctx:
        opt["num_ctx"] = int(num_ctx)
    return opt


def _text_of(m) -> str:
    if isinstance(m, dict):
        return str(m.get("content") or "")
    return str(getattr(m, "content", "") or "")


def _role_of(m) -> str:
    return m.get("role") if isinstance(m, dict) else getattr(m, "role", "")


def _ctx_chars(messages: list) -> int:
    return sum(len(_text_of(m)) for m in messages)


def _head_len(messages: list) -> int:
    """先頭の設定メッセージ(system+作業フォルダ+CLAUDE.md+ack)までの数。"""
    for i, m in enumerate(messages):
        if _role_of(m) == "assistant":
            return i + 1
    return min(len(messages), 1)


def compact_ctx(messages: list, summarizer) -> bool:
    """文脈が大きければ、設定以降を要約1件に置き換える。置換したら True。
    summarizer(transcript:str)->str。構造を壊さないよう設定部分は保持する。"""
    if _ctx_chars(messages) <= CTX_CHAR_LIMIT:
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
        return False
    if not summary:
        return False
    messages[head:] = [{"role": "user", "content": "【これまでの作業の要約(自動圧縮)】\n" + summary}]
    return True


def compact_ctx_with_model(model: str, messages: list, num_ctx: Optional[int] = None) -> bool:
    """モデルを使って文脈を圧縮(必要時のみ)。num_ctx を渡すと要約対象の取りこぼしを防ぐ。"""
    def summarizer(text: str) -> str:
        opt = {"num_predict": 400}
        if num_ctx:
            opt["num_ctx"] = int(num_ctx)
        try:
            r = _client().chat(model=model, messages=[
                {"role": "system", "content": "次の作業ログを、後で作業を再開できるよう日本語で簡潔に要約してください。"
                 "重要な決定・変更したファイル・実行結果・残タスクを箇条書きで。"},
                {"role": "user", "content": text}], options=opt)
            return getattr(r.message, "content", "") or ""
        except Exception:
            return ""
    return compact_ctx(messages, summarizer)


def _change_action(name: str, plan_mode: bool, phase: str, allow_changes: bool,
                   auto_accept_edits: bool = False) -> str:
    """変更系ツールの扱いを返す: 'block'(不可) / 'confirm'(差分つき確認) / 'apply'(自動適用)。
    計画モードで計画が未承認でも、ハードブロックせず1操作ずつ確認に回す
    (present_plan を出さないモデルでも修正を適用できるようにするため)。"""
    if not plan_mode and not allow_changes:
        return "block"               # 非計画モードで変更オフ → 読み取りのみ
    # セッションで「編集を自動適用(acceptEdits)」が選ばれていれば、ファイル編集は確認不要。
    # コマンド系(run_command/run_background)は安全のため引き続き確認する(Claude Code 同様)。
    if auto_accept_edits and name not in CONFIRM_IN_EXEC:
        return "apply"
    if phase != "execute":
        return "confirm"             # 計画未承認 → 個別に差分つきで確認
    if not plan_mode:
        return "confirm"             # 非計画モード(変更許可) → 毎回確認
    if name in CONFIRM_IN_EXEC:
        return "confirm"             # 計画承認後でもコマンド系は確認
    return "apply"                   # 計画承認後のファイル編集 → 自動適用


def _result_status(result: str) -> str:
    """ツール結果の文字列から表示ステータスを判定([エラー] 始まりは失敗扱い)。"""
    return "error" if str(result or "").lstrip().startswith("[エラー]") else "ok"


# ============================================================
#  エージェントループ(イベントを yield)
# ============================================================
def _client():
    return ollama.Client(host=settings.ollama_host)


def _tc_to_dict(tc) -> dict:
    """ツール呼び出し(ollama ToolCall)を、文脈へ積むための dict に変換。"""
    fn = getattr(tc, "function", None)
    name = getattr(fn, "name", "") if fn is not None else ""
    args = getattr(fn, "arguments", {}) if fn is not None else {}
    return {"function": {"name": name, "arguments": args}}


# ============================================================
#  変更の取り消し(undo)と 適用後の自動構文チェック(自己検証)
# ============================================================
_UNDO: dict[str, dict] = {}          # undo_id -> {path(abs), before(str|None), rel}

# 拡張子ごとの「構文だけ」を確かめるコマンド(.pyc を作らない・副作用なし)
_SYNTAX_CMD = {
    ".py": ["python", "-c", "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read())"],
    ".js": ["node", "--check"], ".mjs": ["node", "--check"], ".cjs": ["node", "--check"],
    ".json": ["python", "-c", "import json,sys; json.load(open(sys.argv[1],encoding='utf-8'))"],
}


def _syntax_check(ws: Path, rel: str) -> Optional[str]:
    """編集ファイルの構文を即チェック。エラーなら説明文、問題なし/対象外/ツール無しは None。"""
    cmd = _SYNTAX_CMD.get(Path(rel).suffix.lower())
    if not cmd:
        return None
    try:
        p = _safe_path(ws, rel)
        r = subprocess.run(cmd + [str(p)], cwd=str(ws), capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            return ((r.stderr or "") + (r.stdout or "")).strip()[:1500] or "構文エラー"
    except FileNotFoundError:
        return None                  # node 等が無ければスキップ
    except Exception:
        return None
    return None


def _apply_change(ws: Path, name: str, args: dict, detail: dict):
    """ファイル変更を適用し (result, event) を返す。適用前の内容を undo に控え、
    適用後に構文チェック(自己検証)を行い、失敗はツール結果に追記してモデルへ差し戻す。"""
    rel = args.get("path", "")
    before, captured = None, False
    if name in ("write_file", "edit_file"):
        try:
            p = _safe_path(ws, rel)
            before = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else None
            captured = True
        except Exception:
            captured = False
    result = dispatch(ws, name, args)
    status = _result_status(result)
    undo_id = None
    if name in ("write_file", "edit_file") and status != "error" and captured:
        undo_id = uuid.uuid4().hex[:12]
        _UNDO[undo_id] = {"path": str(_safe_path(ws, rel)), "before": before, "rel": rel}
        serr = _syntax_check(ws, rel)
        if serr:
            result = f"{result}\n[構文エラー] {rel}\n{serr}\n→ 直してください。"
            status = "error"
    ev = {"type": "tool_result", "name": name, "status": status, "result": result}
    if detail.get("diff"):
        ev["diff"] = detail["diff"]
    if detail.get("path"):
        ev["path"] = detail["path"]
    if undo_id:
        ev["undo_id"] = undo_id
    return result, ev


def undo(undo_id: str) -> str:
    """適用済みの変更を取り消す(復元/新規は削除)。1回限り。"""
    info = _UNDO.pop(undo_id, None)
    if not info:
        return "[エラー] 取り消し情報が見つかりません(既に取り消し済み、またはサーバ再起動で失効)"
    p = Path(info["path"])
    try:
        if info["before"] is None:
            if p.is_file():
                p.unlink()
            return f"新規作成を取り消しました(削除): {info['rel']}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(info["before"], encoding="utf-8")
        return f"変更を取り消しました(復元): {info['rel']}"
    except Exception as e:
        return f"[エラー] 取り消しに失敗: {e}"


def run_stream(model: str, messages: list, workspace: str,
               allow_changes: bool, plan_mode: bool = True,
               num_ctx: Optional[int] = None,
               auto_accept_edits: bool = False) -> Iterator[dict]:
    """
    エージェントを1依頼ぶん実行し、イベントを順次 yield する。
    イベント type:
      thinking / assistant_delta / tool_call / tool_result / confirm / plan / todos / done / max_steps / error
    plan_mode=True のときは「調査→present_plan→承認→実行」。
    各ステップの本文は逐次ストリーミング(assistant_delta)で流す。
    """
    ws = Path(workspace).resolve()
    client = _client()
    options = _gen_options(num_ctx)   # コーディング向け生成設定(num_ctx は会話設定を反映)
    phase = "plan" if plan_mode else "execute"
    investigated = False     # 調査(読み取り)系ツールを使ったか
    ask_redirected = False   # 「調査前の聞き返し」を一度リダイレクトしたか
    did_attempt_change = False  # 変更系ツールを一度でも呼んだか
    apply_nudged = False        # 「貼っただけで未適用」の催促を送ったか(1依頼1回)
    # 直近のユーザー依頼に「変更してほしい」意図があるか(安全網の発火条件に使う)
    wants_change = bool(_CHANGE_INTENT.search(
        next((str(m.get("content") or "") for m in reversed(messages)
              if m.get("role") == "user"), "")))

    for _ in range(MAX_STEPS):
        # 実行の途中でも文脈が大きくなったら自動圧縮(溢れ防止。Claude同様)
        try:
            if _ctx_chars(messages) > CTX_CHAR_LIMIT and compact_ctx_with_model(model, messages, num_ctx):
                log.info("文脈を自動圧縮しました(実行中)")
        except Exception:
            log.exception("実行中の文脈圧縮に失敗(無視して続行)")
        tools = PLAN_PHASE_TOOLS if phase == "plan" else EXEC_PHASE_TOOLS
        # --- 1ステップ生成(逐次ストリーミング。失敗時は非ストリームにフォールバック) ---
        content_parts: list[str] = []
        tool_calls: list = []
        streamed = False
        try:
            for chunk in client.chat(model=model, messages=messages, tools=tools,
                                     stream=True, options=options):
                cm = getattr(chunk, "message", None)
                if cm is None:
                    continue
                th = getattr(cm, "thinking", None)
                if th:
                    yield {"type": "thinking", "text": th}   # 思考は表示用に流す(保存はしない)
                c = getattr(cm, "content", None)
                if c:
                    content_parts.append(c)
                    streamed = True
                    yield {"type": "assistant_delta", "text": c}
                for tc in (getattr(cm, "tool_calls", None) or []):
                    tool_calls.append(tc)
        except Exception as e:
            if streamed or tool_calls:
                log.warning("agent ストリーミング中断: %s", e)
                yield {"type": "error", "error": str(e)}
                return
            # まだ何も出力していなければ非ストリームで再試行
            try:
                resp = client.chat(model=model, messages=messages, tools=tools, options=options)
            except Exception as e2:
                emsg = str(e2)
                log.warning("agent 生成失敗: %s", emsg)
                if "tool" in emsg.lower():
                    emsg = (f"{emsg}\n(このモデルはツール呼び出しに未対応かもしれません。"
                            "qwen3 等のツール対応モデルをお試しください)")
                yield {"type": "error", "error": emsg}
                return
            cm = resp.message
            th = getattr(cm, "thinking", None)
            if th:
                yield {"type": "thinking", "text": th}
            c = getattr(cm, "content", None)
            if c:
                content_parts.append(c)
                yield {"type": "assistant_delta", "text": c}
            tool_calls = list(getattr(cm, "tool_calls", None) or [])

        content = "".join(content_parts)
        asst_msg = {"role": "assistant", "content": content}
        if tool_calls:
            asst_msg["tool_calls"] = [_tc_to_dict(tc) for tc in tool_calls]
        messages.append(asst_msg)

        if not tool_calls:
            # 安全網: 変更依頼なのにツールを呼ばず、コードをチャットに貼っただけで終わろうとした
            # 場合は、実ファイルへ適用するよう一度だけ促して継続する(計画モードでない/変更許可時)。
            if (wants_change and not did_attempt_change and not apply_nudged
                    and "```" in content and (allow_changes or plan_mode)):
                apply_nudged = True
                messages.append({"role": "user", "content": _APPLY_NUDGE})
                continue
            yield {"type": "done"}
            return

        for tc in tool_calls:
            name = tc.function.name
            args = tc.function.arguments
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            args = args or {}
            if name in READONLY or name == "summarize_path":
                investigated = True

            # --- 計画の提示と承認 ---
            if name == "present_plan":
                plan_text = (args.get("plan") or content or "(計画なし)").strip()
                aid = new_pending()
                yield {"type": "plan", "action_id": aid, "plan": plan_text}
                decision = wait(aid)
                if decision is True:
                    phase = "execute"
                    result = "[計画承認] 実行フェーズに移行しました。計画に沿って実行してください。"
                    yield {"type": "tool_result", "name": name, "status": "ok", "result": result}
                else:
                    result = ("[計画却下] ユーザーが計画を承認しませんでした。"
                              if decision is False else "[計画承認待ちがタイムアウトしました]")
                    yield {"type": "tool_result", "name": name, "status": "rejected", "result": result}
                    messages.append({"role": "tool", "content": result, "tool_name": name})
                    yield {"type": "done"}     # 追加指示を待つためここで一旦終了
                    return
                messages.append({"role": "tool", "content": result, "tool_name": name})
                continue

            # --- TODO 進捗(メタ操作・常に許可) ---
            if name == "todo_write":
                todos = _norm_todos(args.get("todos"))
                yield {"type": "todos", "todos": todos}
                result = f"[OK] TODOを更新しました({len(todos)}件)"
                messages.append({"role": "tool", "content": result, "tool_name": name})
                continue

            # --- ユーザーへの質問(選択式)。回答を待って続行 ---
            if name == "ask_user":
                # ガード: 調査もせず最初に聞き返すのは「質問の丸投げ」の可能性が高い。
                # 一度だけリダイレクトし、まず自分で調べる/答えるよう促す(無限ループは防ぐ)。
                if not investigated and not ask_redirected:
                    ask_redirected = True
                    result = ("[ガイド] まだ何も調査していません。ユーザーは多くの場合『答え』を求めています。"
                              "聞き返す前に list_files / read_file / grep などで調べ、"
                              "使い方・調べもの・原因調査の質問なら推測せず自分で結論を答えてください。"
                              "本当に作業方針が分岐して結果が変わる場合に限り ask_user を使ってください。")
                    yield {"type": "tool_result", "name": "ask_user", "status": "redirected", "result": result}
                    messages.append({"role": "tool", "content": result, "tool_name": name})
                    continue
                questions = _norm_questions(args)
                context = str(args.get("context") or "").strip()
                aid = new_pending()
                ev = {"type": "ask", "action_id": aid, "questions": questions}
                if context:
                    ev["context"] = context
                yield ev
                ans = wait_answer(aid)   # 質問ごとの選択ラベル配列 / 文字列 / None
                result = _format_ask_result(questions, ans)
                yield {"type": "tool_result", "name": "ask_user", "status": "ok", "result": result}
                messages.append({"role": "tool", "content": result, "tool_name": name})
                continue

            yield {"type": "tool_call", "name": name, "args": _preview_args(name, args)}

            # --- バックグラウンドjobの出力取得・停止(確認不要のメタ操作) ---
            if name in META:
                result = dispatch(ws, name, args)
                yield {"type": "tool_result", "name": name,
                       "status": _result_status(result), "result": result}
                messages.append({"role": "tool", "content": str(result), "tool_name": name})
                continue

            # --- 多数ファイルの map-reduce 要約(読み取りのみ・確認不要) ---
            if name == "summarize_path":
                rel = (args.get("path") or ".").strip() or "."
                instr = (args.get("instruction") or "").strip()
                try:
                    base = _safe_path(ws, rel)
                    from .. import summarize as _summ, rag as _rag
                    from ..defaults import get_defaults as _gd
                    sfiles = _rag.scan_files([str(base)])
                    if not sfiles:
                        result = "[対象ファイルがありません]"
                    else:
                        mm = (_gd().get("summarize_map_model") or "").strip() or None
                        if mm == model:
                            mm = None
                        fn = _summ.model_summarize_fn(model, instr, map_model=mm)
                        result = _summ.run_summarize(sfiles, instr, fn) or "(要約できませんでした)"
                except Exception as e:
                    result = f"[エラー] {e}"
                yield {"type": "tool_result", "name": name,
                       "status": _result_status(result), "result": result}
                messages.append({"role": "tool", "content": str(result), "tool_name": name})
                continue

            if name in MUTATING:
                did_attempt_change = True   # 変更を試みた(=安全網の催促は不要)
                action = _change_action(name, plan_mode, phase, allow_changes, auto_accept_edits)
                if action == "block":
                    result = ("[変更は許可されていません。画面の『変更を許可』をオンにすると、"
                              "承認のうえで変更・実行できます(読み取りは可能です)]")
                    yield {"type": "tool_result", "name": name, "status": "blocked", "result": result}
                elif action == "confirm":
                    # 差分つきで確認カードを出し、承認されたら適用(計画未承認でもここで適用可能)
                    detail = _action_detail(ws, name, args)
                    aid = new_pending()
                    yield {"type": "confirm", "action_id": aid, "name": name, **detail}
                    decision, scope, reason = wait_decision(aid)
                    # 「以後自動適用」が選ばれたら、このセッションのファイル編集は確認を省く
                    if decision is True and scope == "always":
                        auto_accept_edits = True
                    if decision is True:
                        result, ev = _apply_change(ws, name, args, detail)   # 適用+undo控え+構文チェック
                        yield ev
                    elif decision is None:
                        result = "[承認がタイムアウトしたため実行しませんでした]"
                        yield {"type": "tool_result", "name": name, "status": "rejected", "result": result}
                    else:
                        rtxt = (reason or "").strip()
                        result = ("[ユーザーが操作を拒否しました" + (f"。理由: {rtxt}" if rtxt else "") + "]")
                        yield {"type": "tool_result", "name": name, "status": "rejected", "result": result}
                else:
                    # 計画承認済みのファイル編集 → 自動適用(差分を併記して透明性を確保)
                    detail = _action_detail(ws, name, args)
                    result, ev = _apply_change(ws, name, args, detail)   # 適用+undo控え+構文チェック
                    yield ev
            else:
                result = dispatch(ws, name, args)
                yield {"type": "tool_result", "name": name,
                       "status": _result_status(result), "result": result}

            messages.append({"role": "tool", "content": str(result), "tool_name": name})

    yield {"type": "max_steps"}
