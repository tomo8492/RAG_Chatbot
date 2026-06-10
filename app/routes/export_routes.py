"""export_routes.py
回答のファイル出力(Word / Excel / PowerPoint / HTML / テキスト / Markdown / PDF / コード)。
ロジックは export モジュールへ委譲する薄い層。
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from .. import auth, export
from ..logging_setup import get_logger

log = get_logger("export")

router = APIRouter(dependencies=[Depends(auth.require_auth)])


class ExportBody(BaseModel):
    content: str
    format: str = "md"        # md|txt|html|pdf|docx|xlsx|csv|pptx|code
    ext: Optional[str] = None  # format=code のときの拡張子(例: bas)
    title: Optional[str] = "回答"
    images: Optional[list] = None   # PDF用: Mermaid図のPNG(順序対応) [{data(base64), w, h}]
    figures: Optional[list] = None  # 出典の文書内画像 [{data(base64), caption}](参考図として掲載)


@router.post("/api/export")
def api_export(body: ExportBody) -> Response:
    try:
        data, mime, ext = export.export_content(body.content, body.format, body.ext,
                                                body.title or "回答", images=body.images,
                                                figures=body.figures)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except ImportError as e:
        raise HTTPException(500, f"変換に必要なライブラリが未導入です: {e}")
    except Exception as e:
        log.exception("エクスポート失敗")
        raise HTTPException(500, f"変換に失敗しました: {e}")

    fname = f"{export.safe_stem(body.title or '回答')}.{ext}"
    log.info("エクスポート: format=%s -> %s (%d bytes)", body.format, fname, len(data))
    headers = {
        "Content-Disposition": f"attachment; filename=\"export.{ext}\"; filename*=UTF-8''{quote(fname)}",
        "X-Filename": quote(fname),
        "Access-Control-Expose-Headers": "X-Filename",
    }
    return Response(content=data, media_type=mime, headers=headers)
