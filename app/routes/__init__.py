"""routes パッケージ
HTTP ルート層。ドメインごとに `*_routes.py` へ分割していく(odysseus の routes/ を参考)。

main.py は `from .routes import routers` で受け取り、各 router を include する。
新しいドメインを切り出したら、その router を下の `routers` に追加するだけでよい。
"""
from .index_routes import router as index_router

routers = [index_router]
