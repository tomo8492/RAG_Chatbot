"""観測性(リクエストID・アクセスログ・未処理例外ハンドリング)のテスト。

- logging_setup の request_id ヘルパ/フィルタ(純粋)
- _observability ミドルウェア: X-Request-ID 付与 / 未処理例外を 500 JSON + ログ化
pytest でも `python tests/test_observability.py` でも動く。
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import logging_setup  # noqa: E402


# ---------------- request_id ヘルパ / フィルタ ----------------
def test_set_and_get_request_id():
    logging_setup.set_request_id("abc123")
    assert logging_setup.get_request_id() == "abc123"


def test_filter_injects_request_id():
    logging_setup.set_request_id("rid42")
    rec = logging.LogRecord("n", logging.INFO, "f", 1, "msg", None, None)
    assert logging_setup._RequestIdFilter().filter(rec) is True
    assert rec.request_id == "rid42"


# ---------------- ミドルウェア(TestClient) ----------------
def _client():
    from app.config import settings
    for attr, val in [("auth_enabled", False), ("lan_only", False)]:
        try:
            setattr(settings, attr, val)
        except Exception:
            pass
    from app.main import app
    if not any(getattr(r, "path", "") == "/api/_boom_test" for r in app.routes):
        @app.get("/api/_boom_test")
        def _boom():
            raise RuntimeError("boom for test")
    from fastapi.testclient import TestClient
    return TestClient(app, raise_server_exceptions=False)


def test_request_id_header_present_on_normal_request():
    r = _client().get("/healthz")
    assert r.status_code == 200
    assert r.headers.get("X-Request-ID")              # 相関IDが付与される


def test_unhandled_exception_returns_logged_500_json():
    r = _client().get("/api/_boom_test")
    assert r.status_code == 500                        # 未処理例外でもクリーンな500
    body = r.json()
    assert "request_id" in body and r.headers.get("X-Request-ID")  # 痕跡が残る


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            passed += 1
        except AssertionError as e:
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"ERROR {fn.__name__}: {e!r}")
    print(f"{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
