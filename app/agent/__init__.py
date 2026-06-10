"""agent パッケージ(ファサード)。

公開APIは従来どおり ``app.agent.X`` で参照できる(main.py・tests は無改変)。
現在は実装を ``_impl`` に集約しているが、フェーズ2で段階的に
``constants / tools / approvals / preview / context / loop`` へ分割していく。
その際も本ファサードで公開名を維持するため、外部からの参照は変わらない。
"""
from .constants import SYSTEM_PROMPT  # noqa: F401  公開定数(main.py が参照)
from .approvals import resolve, resolve_answer  # noqa: F401  承認/回答(main.py の /api/code/*)
from .helpers import read_project_instructions  # noqa: F401  CLAUDE.md 等の読込(main.py が参照)
from .tools import *  # noqa: F401,F403  ツール(t_*, dispatch, _safe_path)を再エクスポート
from ._impl import *  # noqa: F401,F403  公開名(関数)を再エクスポート
from ._impl import (  # noqa: F401  外部(main.py)/テストが参照する内部名(アンダースコア)
    _UNDO,
    _apply_change,
    _result_status,
    _syntax_check,
)
