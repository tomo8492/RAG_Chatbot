"""agent パッケージ(ファサード)。

公開APIは従来どおり ``app.agent.X`` で参照できる(main.py・tests は無改変)。
現在は実装を ``_impl`` に集約しているが、フェーズ2で段階的に
``constants / tools / approvals / preview / context / loop`` へ分割していく。
その際も本ファサードで公開名を維持するため、外部からの参照は変わらない。
"""
from ._impl import *  # noqa: F401,F403  公開名(関数・定数)を再エクスポート
from ._impl import (  # noqa: F401  外部(main.py)/テストが参照する内部名(アンダースコア)
    _UNDO,
    _apply_change,
    _result_status,
    _safe_path,
    _syntax_check,
)
