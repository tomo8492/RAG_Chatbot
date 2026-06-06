"""app.main の LAN アクセス制限(_ip_allowed / _denied_page)の単体テスト。

社内ネットワーク限定(LAN_ONLY)の判定はセキュリティ上重要なので、ループバック・
プライベート・リンクローカル・IPv4射影 IPv6・公開IP・追加CIDR の各ケースを固定する。
pytest でも `python tests/test_lan_guard.py` 単体実行でも動く。
"""
import contextlib
import ipaddress
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import main  # noqa: E402


@contextlib.contextmanager
def allowed_cidrs(*cidrs):
    """settings.allowed_cidrs を一時的に差し替える。"""
    old = main.settings.allowed_cidrs
    main.settings.allowed_cidrs = [ipaddress.ip_network(c) for c in cidrs]
    try:
        yield
    finally:
        main.settings.allowed_cidrs = old


# ---------------- 許可されるべき ----------------
def test_loopback_allowed():
    assert main._ip_allowed("127.0.0.1")
    assert main._ip_allowed("::1")


def test_localhost_string_allowed():
    assert main._ip_allowed("localhost")


def test_private_ranges_allowed():
    for ip in ("192.168.1.10", "10.0.0.5", "172.16.4.4"):
        assert main._ip_allowed(ip), ip


def test_link_local_allowed():
    assert main._ip_allowed("169.254.1.1")


def test_ipv4_mapped_private_allowed():
    # ::ffff:192.168.x.x はスマホ等で出現する。射影を解いて私設判定する。
    assert main._ip_allowed("::ffff:192.168.1.5")


# ---------------- 拒否されるべき ----------------
def test_public_ipv4_denied():
    assert not main._ip_allowed("8.8.8.8")
    assert not main._ip_allowed("1.1.1.1")


def test_ipv4_mapped_public_denied():
    assert not main._ip_allowed("::ffff:8.8.8.8")


def test_empty_and_garbage_denied():
    assert not main._ip_allowed(None)
    assert not main._ip_allowed("")
    assert not main._ip_allowed("not-an-ip")


# ---------------- 追加 CIDR(.env の ALLOWED_CIDRS 相当) ----------------
def test_extra_cidr_allows_otherwise_public_ip():
    assert not main._ip_allowed("9.9.9.9")              # 既定(公開IP)では拒否
    with allowed_cidrs("9.9.9.0/24"):
        assert main._ip_allowed("9.9.9.9")              # 追加すると許可


def test_extra_cidr_does_not_leak_other_ips():
    with allowed_cidrs("9.9.9.0/24"):
        assert not main._ip_allowed("8.8.8.8")          # 範囲外の公開IPは依然拒否


# ---------------- 拒否ページ ----------------
def test_denied_page_shows_ip_and_cidr_hint():
    page = main._denied_page("203.0.113.9")
    assert "203.0.113.9" in page
    assert "203.0.113.0/24" in page             # 管理者向けの CIDR ヒント


def test_denied_page_escapes_angle_brackets():
    page = main._denied_page("<script>")
    assert "<script>" not in page


def test_denied_page_mapped_ip_hint():
    page = main._denied_page("::ffff:192.168.1.5")
    assert "192.168.1.0/24" in page


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
