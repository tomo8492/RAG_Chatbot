"""自律検証ループ(Claude Code 風)用のヘルパ。

  - detect_verify_cmd: 作業フォルダの構成から検証コマンドを推定
  - run_verify: 検証コマンドを実行し (成功か, 出力) を返す

run_stream はファイル変更後に「検証 → 失敗ならモデルへ差し戻して修正 → 再検証」を
最大 MAX_VERIFY_ROUNDS 回まで自動で回す。本モジュールはその実行部品(副作用は
サブプロセス実行のみで、作業フォルダ内で完結する)。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .constants import CMD_TIMEOUT
from ..logging_setup import get_logger

log = get_logger("agent.verify")


def _has(ws: Path, *names: str) -> bool:
    return any((ws / n).exists() for n in names)


def detect_verify_cmds(ws: Path) -> list[str]:
    """作業フォルダから検証コマンド群を推定する(テスト/ビルドに加え lint・型チェックも)。

    「動くか(テスト/ビルド)」だけでなく「綺麗か(lint/型)」も自動で回し、生成コードの
    品質を底上げする。検出できたものだけを返す(空なら [])。明示指定があればそちらを優先。
    """
    cmds: list[str] = []
    try:
        py_proj = _has(ws, "pyproject.toml", "setup.cfg", "tox.ini", "pytest.ini") \
            or (ws / "tests").is_dir() or (ws / "test").is_dir()
        if py_proj:
            cmds.append("pytest -q")
            # lint/型は「そのプロジェクトが使っている証拠」があるときだけ追加
            # (無関係な環境で誤って失敗→自律ループが幻のエラーを追うのを防ぐ)
            if _has(ws, "ruff.toml", ".ruff.toml") or _mentions(ws, "pyproject.toml", "ruff") \
                    or _mentions(ws, "setup.cfg", "ruff") or _mentions(ws, "tox.ini", "ruff"):
                cmds.append("ruff check .")
            if _has(ws, "mypy.ini", ".mypy.ini") or _mentions(ws, "pyproject.toml", "mypy") \
                    or _mentions(ws, "setup.cfg", "mypy"):
                cmds.append("mypy .")
        pj = ws / "package.json"
        if pj.is_file():
            scripts: dict = {}
            try:
                scripts = (json.loads(pj.read_text(encoding="utf-8")) or {}).get("scripts") or {}
            except Exception:
                log.debug("detect_verify_cmds: package.json 解析失敗", exc_info=True)
            if "test" in scripts:
                cmds.append("npm test --silent")
            elif "build" in scripts:
                cmds.append("npm run build")
            if "lint" in scripts:
                cmds.append("npm run lint --silent")
            if "typecheck" in scripts:
                cmds.append("npm run typecheck --silent")
            elif _has(ws, "tsconfig.json"):
                cmds.append("npx --no-install tsc --noEmit")
        if (ws / "go.mod").is_file():
            cmds.append("go build ./...")
            cmds.append("go vet ./...")
        if not cmds and (_has(ws, "Makefile", "makefile")):
            cmds.append("make test")
    except Exception:
        log.debug("detect_verify_cmds: 例外を無視して継続", exc_info=True)
    # 重複除去(順序維持)
    out: list[str] = []
    for c in cmds:
        if c not in out:
            out.append(c)
    return out


def _mentions(ws: Path, fname: str, token: str) -> bool:
    """設定ファイルに特定トークン(例: mypy)が含まれるか。読めなければ False。"""
    try:
        p = ws / fname
        return p.is_file() and token in p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        log.debug("_mentions: 例外を無視して継続", exc_info=True)
        return False


def detect_verify_cmd(ws: Path) -> str:
    """後方互換: 推定した検証コマンドの先頭1件(無ければ '')。"""
    cmds = detect_verify_cmds(ws)
    return cmds[0] if cmds else ""


def _looks_missing_tool(returncode: int, out: str) -> bool:
    """実行ファイルが見つからない系の失敗か(検証ツール未導入の判定)。"""
    low = (out or "").lower()
    markers = ("command not found", "not found", "is not recognized",
               "no such file or directory", "could not find", "ためのファイルが見つかりません")
    return (returncode == 127) or any(m in low for m in markers)


def run_verify(ws: Path, cmd: str, timeout: int = CMD_TIMEOUT) -> tuple[bool, str]:
    """検証コマンドを作業フォルダで実行。戻り: (成功か, 表示用の出力)。

    空コマンドや実行失敗・タイムアウトは (False, 理由) を返す。
    """
    cmd = (cmd or "").strip()
    if not cmd:
        return False, "[エラー] 検証コマンドが空です"
    try:
        r = subprocess.run(cmd, shell=True, cwd=str(ws),
                           capture_output=True, text=True, timeout=timeout)
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        # ツール未導入(command not found 等)は失敗ではなく「スキップ」扱いにし、
        # 自律検証ループが存在しないエラーを追いかけないようにする。
        if _looks_missing_tool(r.returncode, out):
            return True, f"[スキップ] 検証ツールが未導入のため省略: {cmd}"
        out = out[:6000] + ("\n...(出力省略)" if len(out) > 6000 else "")
        head = "[検証OK]" if r.returncode == 0 else f"[検証失敗 終了コード {r.returncode}]"
        return r.returncode == 0, f"{head}\n{out or '(出力なし)'}"
    except subprocess.TimeoutExpired:
        log.debug("run_verify: タイムアウト", exc_info=True)
        return False, f"[エラー] 検証がタイムアウトしました({timeout}秒)"
    except Exception as e:
        log.debug("run_verify: 例外", exc_info=True)
        return False, f"[エラー] 検証の実行に失敗: {e}"


def resolve_verify_cmds(ws: Path, verify_cmd: str) -> list[str]:
    """実行する検証コマンド一覧を決める。設定(verify_cmd・改行区切りで複数可)を優先し、
    空なら作業フォルダから自動検出した1件。見つからなければ空リスト。"""
    cmds = [c.strip() for c in (verify_cmd or "").splitlines() if c.strip()]
    if cmds:
        return cmds
    return detect_verify_cmds(ws)


def run_checks(ws: Path, cmds: list[str]) -> tuple[bool, str]:
    """複数の検証コマンドを順に実行し、(すべて成功か, 連結した出力) を返す。"""
    if not cmds:
        return False, "[エラー] 実行する検証コマンドがありません"
    all_ok = True
    blocks: list[str] = []
    for c in cmds:
        ok, out = run_verify(ws, c)
        blocks.append(f"$ {c}\n{out}")
        if not ok:
            all_ok = False
    return all_ok, "\n\n".join(blocks)
