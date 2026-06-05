"""
config.py
環境変数 / .env から設定を読み込む。アプリ全体で共有する設定オブジェクトを提供する。
"""
from __future__ import annotations

import os
import ipaddress
import secrets
from pathlib import Path

from dotenv import load_dotenv

# プロジェクトルート( app/ の1つ上 )
ROOT_DIR = Path(__file__).resolve().parent.parent

# .env を読み込む(存在すれば)
load_dotenv(ROOT_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip())
    except (ValueError, AttributeError):
        return default


class Settings:
    """アプリ設定。起動時に1度だけ生成する。"""

    def __init__(self) -> None:
        # --- サーバ ---
        self.host: str = os.getenv("HOST", "0.0.0.0").strip()
        self.port: int = _int("PORT", 8800)
        # ローカルLAN/ループバック以外(=インターネット側のグローバルIP)からのアクセスを拒否
        self.lan_only: bool = _bool("LAN_ONLY", True)
        # LAN_ONLY 有効時に、プライベートIPに加えて許可する追加ネットワーク(CIDR, カンマ区切り)。
        # 社内が 172.36.x.x などの非標準レンジの場合に ALLOWED_CIDRS=172.36.0.0/16 のように指定する。
        self.allowed_cidrs = self._parse_cidrs(os.getenv("ALLOWED_CIDRS", ""))

        # --- データ保存先 ---
        data_dir = os.getenv("DATA_DIR", "data").strip()
        self.data_dir: Path = (ROOT_DIR / data_dir) if not os.path.isabs(data_dir) else Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.db_path: Path = self.data_dir / "app.db"
        self.chroma_dir: Path = self.data_dir / "chroma"
        self.upload_dir: Path = self.data_dir / "uploads"
        self.upload_dir.mkdir(parents=True, exist_ok=True)

        # --- 認証 ---
        self.password: str = os.getenv("CHAT_PASSWORD", "").strip()
        self.auth_enabled: bool = bool(self.password)
        self.secret_key: str = self._load_secret_key(os.getenv("SECRET_KEY", "").strip())

        # --- Ollama ---
        self.ollama_host: str = os.getenv("OLLAMA_HOST", "http://localhost:11434").strip()
        self.chat_model: str = os.getenv("CHAT_MODEL", "gemma3:27b").strip()
        # 画像(スクショ)付き質問のときに使う Vision モデル。
        # Gemma 3 はマルチモーダルなので既定では同じモデルを使用(切替なし)。
        self.vision_model: str = os.getenv("VISION_MODEL", "gemma3:27b").strip()
        # OCR API(/api/ocr)を VBA / Python など外部から呼ぶときの簡易APIキー。
        # CHAT_PASSWORD を設定して認証を有効にしている場合、Cookieの代わりに
        # X-API-Key ヘッダでこのキーを送れば呼び出せる(VBA等からの利用向け)。
        self.ocr_api_key: str = os.getenv("OCR_API_KEY", "").strip()

        # --- 埋め込み ---
        self.embed_backend: str = os.getenv("EMBED_BACKEND", "sentence-transformers").strip().lower()
        default_embed = (
            "intfloat/multilingual-e5-small"
            if self.embed_backend == "sentence-transformers"
            else "nomic-embed-text"
        )
        self.embed_model: str = os.getenv("EMBED_MODEL", default_embed).strip()

        # --- RAG ---
        self.rag_top_k: int = _int("RAG_TOP_K", 15)
        self.chunk_size: int = _int("CHUNK_SIZE", 800)
        self.chunk_overlap: int = _int("CHUNK_OVERLAP", 120)

        # --- OCR(スキャン/画像PDF。既定OFF=現行動作を変えない) ---
        self.ocr_enabled: bool = _bool("OCR_ENABLED", False)
        # エンジン: "vlm"(Ollamaのビジョンモデル) / "tesseract"
        self.ocr_engine: str = os.getenv("OCR_ENGINE", "vlm").strip().lower()
        # VLM用モデル(空なら vision_model を流用)。例: "qwen2.5vl"
        self.ocr_vlm_model: str = os.getenv("OCR_VLM_MODEL", "").strip()
        # ページのテキスト層がこの文字数未満なら「スキャン頁」とみなしOCRへ回す
        self.ocr_min_chars: int = _int("OCR_MIN_CHARS", 16)
        self.ocr_lang: str = os.getenv("OCR_LANG", "jpn+eng").strip()   # tesseract用(日英)
        self.ocr_dpi: int = _int("OCR_DPI", 200)                        # 頁→画像の解像度

        # --- アップロード ---
        self.max_upload_mb: int = _int("MAX_UPLOAD_MB", 50)

        # --- 表示 ---
        self.app_title: str = os.getenv("APP_TITLE", "社内文書アシスタント").strip()

    @staticmethod
    def _parse_cidrs(raw: str) -> list:
        """カンマ区切りの CIDR/IP 文字列を ip_network のリストに変換(不正値は無視)。"""
        nets = []
        for part in (raw or "").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                nets.append(ipaddress.ip_network(part, strict=False))
            except ValueError:
                pass
        return nets

    def _load_secret_key(self, env_value: str) -> str:
        """SECRET_KEY が指定されていなければ自動生成して data/secret.key に保存する。"""
        if env_value:
            return env_value
        key_file = self.data_dir / "secret.key"
        if key_file.exists():
            return key_file.read_text(encoding="utf-8").strip()
        key = secrets.token_hex(32)
        key_file.write_text(key, encoding="utf-8")
        return key


# シングルトン
settings = Settings()
