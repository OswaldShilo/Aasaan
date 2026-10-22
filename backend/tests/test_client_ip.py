"""Tests for client IP extraction, X-Forwarded-For parsing, and /64 IPv6 rate limiting."""

import os
from unittest.mock import patch
import pytest
from starlette.requests import Request
from fastapi.testclient import TestClient

from app.client_ip import get_client_ip, get_rate_limit_key, _parse_candidate
from app.main import create_app


def make_request(xff: str | None = None, peer_ip: str = "10.0.0.99", peer_port: int = 54321) -> Request:
    """Helper to construct a Request with custom X-Forwarded-For and peer address."""
    headers = []
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode("utf-8")))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": headers,
        "client": (peer_ip, peer_port) if peer_ip else None,
    }
    return Request(scope)


# ------------------------------------------------------------------------------
# 1. Candidate IP parser tests
# ------------------------------------------------------------------------------
def test_parse_candidate_clean_ips():
    assert _parse_candidate("192.168.1.1") == "192.168.1.1"
    assert _parse_candidate("203.0.113.195") == "203.0.113.195"
    assert _parse_candidate("::1") == "::1"
    assert _parse_candidate("2001:db8::1") == "2001:db8::1"


def test_parse_candidate_with_ports_and_brackets():
    # IPv4 with port
    assert _parse_candidate("192.168.1.1:8080") == "192.168.1.1"
    assert _parse_candidate("1.2.3.4:5678") == "1.2.3.4"
    # Bracketed IPv6 with port
    assert _parse_candidate("[::1]:80") == "::1"
    assert _parse_candidate("[2001:db8::1]:443") == "2001:db8::1"
    # Bracketed IPv6 without port
    assert _parse_candidate("[2001:db8::1]") == "2001:db8::1"
    # Whitespace and quotes
    assert _parse_candidate('  "192.168.1.1:8080"  ') == "192.168.1.1"
    assert _parse_candidate(" '[::1]:80' ") == "::1"


def test_parse_candidate_malformed_values_never_raise():
    assert _parse_candidate("garbage") is None
    assert _parse_candidate("1.2.3.4.5") is None
    assert _parse_candidate("999.1.1.1") is None
    assert _parse_candidate("1.2.3.4:notaport") is None
    assert _parse_candidate("1.2.3.4:0") is None
    assert _parse_candidate("1.2.3.4:999999") is None
    assert _parse_candidate("[invalid_ipv6]:80") is None
    assert _parse_candidate("[::1]:notaport") is None
    assert _parse_candidate("huge_string_" + "a" * 500) is None
    assert _parse_candidate("१.२.३.४") is None  # unicode numerals
    assert _parse_candidate("") is None
    assert _parse_candidate(None) is None


# ------------------------------------------------------------------------------
# 2. Proxy hop trust model tests
# ------------------------------------------------------------------------------
def test_hops_1_uses_rightmost_entry_and_ignores_forged_left():
    with patch.dict(os.environ, {"AASAAN_TRUSTED_PROXY_HOPS": "1"}):
        req = make_request(xff="1.1.1.1, 203.0.113.5", peer_ip="10.0.0.1")
        assert get_client_ip(req) == "203.0.113.5"

        # Multiple forged entries on the left
        req2 = make_request(xff="127.0.0.1, 8.8.8.8, 1.2.3.4, 198.51.100.42", peer_ip="10.0.0.1")
        assert get_client_ip(req2) == "198.51.100.42"


def test_hops_2_uses_second_from_right():
    with patch.dict(os.environ, {"AASAAN_TRUSTED_PROXY_HOPS": "2"}):
        # Format: forged_client, real_client, intermediary_proxy
        req = make_request(xff="1.1.1.1, 203.0.113.77, 10.100.0.5", peer_ip="10.0.0.1")
        assert get_client_ip(req) == "203.0.113.77"


def test_hops_0_ignores_proxy_headers():
    with patch.dict(os.environ, {"AASAAN_TRUSTED_PROXY_HOPS": "0"}):
        req = make_request(xff="203.0.113.5", peer_ip="10.0.0.99")
        assert get_client_ip(req) == "10.0.0.99"


def test_missing_empty_and_too_few_entries_fall_back_to_peer():
    with patch.dict(os.environ, {"AASAAN_TRUSTED_PROXY_HOPS": "1"}):
        # Missing header
        req1 = make_request(xff=None, peer_ip="10.0.0.88")
        assert get_client_ip(req1) == "10.0.0.88"

        # Empty or whitespace header
        req2 = make_request(xff="", peer_ip="10.0.0.88")
        assert get_client_ip(req2) == "10.0.0.88"
        req3 = make_request(xff="   ,  ,  ", peer_ip="10.0.0.88")
        assert get_client_ip(req3) == "10.0.0.88"

    with patch.dict(os.environ, {"AASAAN_TRUSTED_PROXY_HOPS": "3"}):
        # Only 2 entries when 3 are required
        req4 = make_request(xff="1.1.1.1, 2.2.2.2", peer_ip="10.0.0.88")
        assert get_client_ip(req4) == "10.0.0.88"


def test_malformed_chosen_entry_falls_back_safely():
    with patch.dict(os.environ, {"AASAAN_TRUSTED_PROXY_HOPS": "1"}):
        req = make_request(xff="1.1.1.1, malformed_garbage_data", peer_ip="10.0.0.77")
        assert get_client_ip(req) == "10.0.0.77"


# ------------------------------------------------------------------------------
# 3. Rate limit key derivation & IPv6 /64 prefix bucketing
# ------------------------------------------------------------------------------
def test_ipv4_rate_limit_key():
    req = make_request(xff="203.0.113.10")
    assert get_rate_limit_key(req) == "203.0.113.10"


def test_ipv6_rate_limit_key_buckets_per_64():
    # Two addresses within the same /64 prefix
    req1 = make_request(xff="2001:db8:abcd:0012:0000:0000:0000:0001")
    req2 = make_request(xff="2001:db8:abcd:0012:ffff:eeee:dddd:cccc")
    key1 = get_rate_limit_key(req1)
    key2 = get_rate_limit_key(req2)
    assert key1 == "2001:db8:abcd:12::/64"
    assert key2 == "2001:db8:abcd:12::/64"
    assert key1 == key2

    # An address in a different /64 prefix
    req3 = make_request(xff="2001:db8:abcd:0013::1")
    key3 = get_rate_limit_key(req3)
    assert key3 == "2001:db8:abcd:13::/64"
    assert key1 != key3


# ------------------------------------------------------------------------------
# 4. Integration tests with FastAPI application and rate limiting
# ------------------------------------------------------------------------------
from fake_provider import FakeProvider


@pytest.fixture
def make_app_client(tmp_path):
    def _make(nb_per_hour: int = 12):
        db_file = str(tmp_path / f"test_{nb_per_hour}.db")
        with patch.dict(os.environ, {"AASAAN_NB_PER_HOUR": str(nb_per_hour)}):
            app = create_app(db_path=db_file, start_worker=False, self_test_on_start=False, provider_override=FakeProvider())
            return TestClient(app)
    return _make


def test_admin_token_localhost_check_cannot_be_bypassed_with_forged_leftmost_ip(make_app_client):
    client = make_app_client()
    with patch.dict(os.environ, {"AASAAN_TRUSTED_PROXY_HOPS": "1"}, clear=False):
        os.environ.pop("AASAAN_ADMIN_TOKEN", None)
        # Attempt to bypass require_admin by forging localhost on the left
        resp = client.get("/api/events", headers={"X-Forwarded-For": "127.0.0.1, 198.51.100.22"})
        # Should be rejected because rightmost IP is 198.51.100.22 (not localhost)
        assert resp.status_code == 403
        assert "localhost only" in resp.text

        # If rightmost IS 127.0.0.1 (actual localhost proxy), it is allowed
        resp_local = client.get("/api/events", headers={"X-Forwarded-For": "198.51.100.22, 127.0.0.1"})
        assert resp_local.status_code == 200


def test_rate_limiter_spoofed_leftmost_shares_single_bucket(make_app_client):
    client = make_app_client(nb_per_hour=2)
    lid = client.post("/api/learners").json()["learner_id"]
    data = {"learner_id": lid, "text": "I want to learn DSA", "language": "en"}

    # Real client is 203.0.113.50, but client forges different leftmost IPs each time
    h1 = {"X-Forwarded-For": "1.1.1.1, 203.0.113.50"}
    h2 = {"X-Forwarded-For": "2.2.2.2, 203.0.113.50"}
    h3 = {"X-Forwarded-For": "3.3.3.3, 203.0.113.50"}

    r1 = client.post("/api/notebooks", data=data, headers=h1)
    assert r1.status_code == 200

    r2 = client.post("/api/notebooks", data=data, headers=h2)
    assert r2.status_code == 200

    # 3rd request from same rightmost client must trigger 429 despite forged leftmost IP
    r3 = client.post("/api/notebooks", data=data, headers=h3)
    assert r3.status_code == 429


def test_rate_limiter_different_rightmost_get_separate_buckets(make_app_client):
    client = make_app_client(nb_per_hour=1)
    lid = client.post("/api/learners").json()["learner_id"]
    data = {"learner_id": lid, "text": "I want to learn DSA", "language": "en"}

    # Client A: 198.51.100.1
    r1 = client.post("/api/notebooks", data=data, headers={"X-Forwarded-For": "198.51.100.1"})
    assert r1.status_code == 200

    # Client A again -> 429
    r1_blocked = client.post("/api/notebooks", data=data, headers={"X-Forwarded-For": "198.51.100.1"})
    assert r1_blocked.status_code == 429

    # Client B: 198.51.100.2 -> separate bucket, must succeed
    r2 = client.post("/api/notebooks", data=data, headers={"X-Forwarded-For": "198.51.100.2"})
    assert r2.status_code == 200


def test_rate_limiter_ipv6_within_same_64_shares_bucket(make_app_client):
    client = make_app_client(nb_per_hour=1)
    lid = client.post("/api/learners").json()["learner_id"]
    data = {"learner_id": lid, "text": "I want to learn DSA", "language": "en"}

    # First address in 2001:db8:ffff:1::/64
    r1 = client.post("/api/notebooks", data=data, headers={"X-Forwarded-For": "2001:db8:ffff:1::10"})
    assert r1.status_code == 200

    # Second address in the same /64: 2001:db8:ffff:1::99 -> blocked by 429
    r2 = client.post("/api/notebooks", data=data, headers={"X-Forwarded-For": "2001:db8:ffff:1::99"})
    assert r2.status_code == 429

    # Third address in DIFFERENT /64: 2001:db8:ffff:2::10 -> allowed
    r3 = client.post("/api/notebooks", data=data, headers={"X-Forwarded-For": "2001:db8:ffff:2::10"})
    assert r3.status_code == 200
