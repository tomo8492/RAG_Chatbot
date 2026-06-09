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
import sys
import uuid
from pathlib import Path
from typing import Iterator, Optional

import ollama

from ..config import settings
from ..logging_setup import get_logger
from .constants import (
    CTX_CHAR_LIMIT,
    CONFIRM_IN_EXEC,
    EXEC_PHASE_TOOLS,
    MAX_STEPS,
    MAX_VERIFY_ROUNDS,
    META,
    MUTATING,
    PLAN_PHASE_TOOLS,
    READONLY,
    _APPLY_NUDGE,
    _CHANGE_INTENT,
)
from .tools import dispatch, _safe_path
from .verify import detect_verify_cmd, run_verify
from .approvals import new_pending, wait, wait_answer, wait_decision
from .context import _ctx_chars, compact_ctx
from .helpers import _norm_todos, _norm_questions, _format_ask_result

log = get_logger("agent")


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
        log.debug("_change_preview: 例外を無視して継続", exc_info=True)
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
            log.debug("summarizer: 例外を無視して継続", exc_info=True)
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


def _mark_read(ws: Path, rel: str, read_files: set) -> None:
    """read_file 済み・変更成功したファイルを記録(編集前 read のゲート判定に使う)。"""
    try:
        read_files.add(str(_safe_path(ws, rel)))
    except Exception:
        log.debug("_mark_read: 例外を無視して継続", exc_info=True)


def _require_read_first(ws: Path, name: str, args: dict, read_files: set) -> Optional[str]:
    """編集前に read_file を要求するゲート。未読で読むべきなら理由文字列、OKなら None。
    - edit_file: 既存ファイルの部分置換 → 必ず事前 read を要求(盲目編集での行重複/インデント崩れを防ぐ)
    - write_file: 既存ファイルの全文上書きのときだけ要求(新規作成は不要)
    """
    rel = (args.get("path") or "").strip()
    if not rel:
        return None
    try:
        p = _safe_path(ws, rel)
    except Exception:
        return None  # パス不正は各ツール側で本来のエラーにさせる
    if str(p) in read_files or not p.is_file():
        return None  # 既読、または新規作成(存在しない)は通す
    if name == "edit_file":
        return (f"[要 read_file] {rel} をまだ読んでいません。edit_file の前に read_file で"
                "現在の内容を確認してください(古い記憶や推測での編集は行の重複・インデント崩れの原因になります)。")
    if name == "write_file":
        return (f"[要 read_file] {rel} は既存ファイルです。全文上書きの前に read_file で"
                "現在の内容を確認してください(誤上書き防止)。")
    return None


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
    # .py/.json は実行中のインタプリタ(sys.executable)で検査する。Windows で PATH に
    # "python" が無くてもスキップされず、構文エラーを確実に検出してモデルへ差し戻せる。
    ".py": [sys.executable, "-c", "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read())"],
    ".js": ["node", "--check"], ".mjs": ["node", "--check"], ".cjs": ["node", "--check"],
    ".json": [sys.executable, "-c", "import json,sys; json.load(open(sys.argv[1],encoding='utf-8'))"],
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
        log.debug("_syntax_check: 例外を無視して継続", exc_info=True)
        return None                  # node 等が無ければスキップ
    except Exception:
        log.debug("_syntax_check: 例外を無視して継続", exc_info=True)
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
            log.debug("_apply_change: 例外を無視して継続", exc_info=True)
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
        log.debug("undo: 例外を無視して継続", exc_info=True)
        return f"[エラー] 取り消しに失敗: {e}"


def run_stream(model: str, messages: list, workspace: str,
               allow_changes: bool, plan_mode: bool = True,
               num_ctx: Optional[int] = None,
               auto_accept_edits: bool = False,
               auto_verify: bool = False, verify_cmd: str = "") -> Iterator[dict]:
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
    read_files: set[str] = set()   # この依頼で read_file 済みのファイル(編集前 read の強制に使う)
    applied_change = False         # ファイル変更を実際に適用したか(自律検証ループの起動条件)
    verify_rounds = 0              # 自動検証の実行回数
    verify_passed = False          # 検証が成功したか

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
            # --- 自律検証ループ(Claude Code 風): 変更したのに未検証なら、検証コマンドを
            #     自動実行して、失敗ならモデルに差し戻して直させる(最大 MAX_VERIFY_ROUNDS 回)。
            #     表示は通常のツール(verify)として流すので、フロントは既存のツール表示で描ける。
            if (auto_verify and applied_change and not verify_passed
                    and verify_rounds < MAX_VERIFY_ROUNDS):
                vcmd = (verify_cmd or detect_verify_cmd(ws)).strip()
                if vcmd:
                    verify_rounds += 1
                    yield {"type": "tool_call", "name": "verify",
                           "args": {"command": vcmd, "round": f"{verify_rounds}/{MAX_VERIFY_ROUNDS}"}}
                    passed, vout = run_verify(ws, vcmd)
                    yield {"type": "tool_result", "name": "verify",
                           "status": "ok" if passed else "error", "result": vout}
                    if not passed:
                        messages.append({"role": "user", "content": (
                            f"[自動検証] `{vcmd}` が失敗しました(試行 {verify_rounds}/{MAX_VERIFY_ROUNDS})。"
                            "出力を読んで原因のファイルを特定し、修正してください。"
                            f"修正後に自動で再検証します。\n{vout}")})
                        continue
                    verify_passed = True
            yield {"type": "done"}
            return

        for tc in tool_calls:
            name = tc.function.name
            args = tc.function.arguments
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    log.debug("run_stream: 例外を無視して継続", exc_info=True)
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
                    log.debug("run_stream: 例外を無視して継続", exc_info=True)
                    result = f"[エラー] {e}"
                yield {"type": "tool_result", "name": name,
                       "status": _result_status(result), "result": result}
                messages.append({"role": "tool", "content": str(result), "tool_name": name})
                continue

            if name in MUTATING:
                did_attempt_change = True   # 変更を試みた(=安全網の催促は不要)
                gate = _require_read_first(ws, name, args, read_files)
                if gate:   # 未読ファイルの盲目編集を防ぐ(まず read_file させる)
                    yield {"type": "tool_result", "name": name, "status": "error", "result": gate}
                    messages.append({"role": "tool", "content": gate, "tool_name": name})
                    continue
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
                if name == "read_file" and _result_status(result) != "error":
                    _mark_read(ws, args.get("path", ""), read_files)   # 読了を記録(編集ゲート用)
                yield {"type": "tool_result", "name": name,
                       "status": _result_status(result), "result": result}

            # 変更が成功したファイルは「読んだ」とみなす(以後の同一ファイル編集はゲートを通す)
            if name in ("write_file", "edit_file") and _result_status(result) != "error":
                _mark_read(ws, args.get("path", ""), read_files)
                applied_change = True   # 自律検証ループの起動条件
            messages.append({"role": "tool", "content": str(result), "tool_name": name})

    yield {"type": "max_steps"}
