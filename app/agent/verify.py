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


def detect_verify_cmd(ws: Path) -> str:
    """作業フォルダから検証コマンドを推定する。見つからなければ ''(空)。

    優先: Python(pytest) → Node(npm test/build) → Go(build) → Makefile(test)。
    あくまで推定なので、明示指定(verify_cmd)があればそちらを優先する。
    """
    try:
        if (any((ws / n).is_file() for n in ("pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg"))
                or (ws / "tests").is_dir() or (ws / "test").is_dir()):
            return "pytest -q"
        pj = ws / "package.json"
        if pj.is_file():
            scripts = (json.loads(pj.read_text(encoding="utf-8")) or {}).get("scripts") or {}
            if "test" in scripts:
                return "npm test --silent"
            if "build" in scripts:
                return "npm run build"
        if (ws / "go.mod").is_file():
            return "go build ./..."
        if (ws / "Makefile").is_file() or (ws / "makefile").is_file():
            return "make test"
    except Exception:
        log.debug("detect_verify_cmd: 例外を無視して継続", exc_info=True)
    return ""


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
        out = out[:6000] + ("\n...(出力省略)" if len(out) > 6000 else "")
        head = "[検証OK]" if r.returncode == 0 else f"[検証失敗 終了コード {r.returncode}]"
        return r.returncode == 0, f"{head}\n{out or '(出力なし)'}"
    except subprocess.TimeoutExpired:
        log.debug("run_verify: タイムアウト", exc_info=True)
        return False, f"[エラー] 検証がタイムアウトしました({timeout}秒)"
    except Exception as e:
        log.debug("run_verify: 例外", exc_info=True)
        return False, f"[エラー] 検証の実行に失敗: {e}"
