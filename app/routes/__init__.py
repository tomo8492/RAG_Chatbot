"""routes パッケージ
HTTP ルート層。ドメインごとに `*_routes.py` へ分割していく(odysseus の routes/ を参考)。

main.py は `from .routes import routers` で受け取り、各 router を include する。
新しいドメインを切り出したら、その router を下の `routers` に追加するだけでよい。
"""
from .conversation_routes import router as conversation_router
from .export_routes import router as export_router
from .fs_routes import router as fs_router
from .index_routes import router as index_router
from .meta_routes import router as meta_router
from .ocr_routes import router as ocr_router

routers = [
    meta_router,
    conversation_router,
    index_router,
    fs_router,
    export_router,
    ocr_router,
]
