# covers: custom_alias, expiry
"""Contract-level tests for custom aliases and link expiry (fresh databases)."""
import pytest
from fastapi.testclient import TestClient

from shortener.api import create_app
from shortener.config import Settings
from shortener.validation import InvalidAlias, validate_alias

DAY = 86_400


def _post(client, **body):
    body.setdefault("url", "https://example.com/page")
    return client.post("/api/links", json=body)


# ---- aliases -------------------------------------------------------------------------
def test_alias_is_used_as_the_code(client):
    r = _post(client, alias="my-link_1")
    assert r.status_code == 201
    assert r.json()["code"] == "my-link_1"
    assert r.json()["short_url"].endswith("/my-link_1")
    redirect = client.get("/my-link_1", follow_redirects=False)
    assert redirect.status_code == 307
    assert redirect.headers["location"] == "https://example.com/page"


def test_duplicate_alias_returns_409_and_keeps_original(client):
    assert _post(client, alias="taken", url="https://first.example").status_code == 201
    r = _post(client, alias="taken", url="https://second.example")
    assert r.status_code == 409
    again = client.get("/taken", follow_redirects=False)
    assert again.headers["location"] == "https://first.example"


@pytest.mark.parametrize("alias", ["ab", "x" * 33, "has space", "dot.ted", "slash/y", "emoji\U0001F600x"])
def test_invalid_alias_shapes_are_422(client, alias):
    assert _post(client, alias=alias).status_code == 422


@pytest.mark.parametrize("alias", ["api", "healthz", "orchestrator", "ADMIN", "Docs"])
def test_reserved_aliases_are_rejected_case_insensitively(client, alias):
    assert _post(client, alias=alias).status_code == 422


def test_reserved_routes_still_work_after_alias_feature(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_validate_alias_unit():
    assert validate_alias("abc") == "abc"
    with pytest.raises(InvalidAlias):
        validate_alias("a")
    with pytest.raises(InvalidAlias):
        validate_alias(None)


def test_links_without_alias_still_get_random_codes(client):
    code = _post(client).json()["code"]
    assert len(code) == 7


# ---- expiry --------------------------------------------------------------------------
def test_link_without_expiry_never_expires(client):
    r = _post(client)
    assert r.json()["expires_at"] is None


def test_expires_at_is_created_at_plus_days(client):
    body = _post(client, expires_in_days=2).json()
    assert body["expires_at"] == pytest.approx(body["created_at"] + 2 * DAY, abs=1)


@pytest.mark.parametrize("days", [0, -1, 366, 1000])
def test_out_of_range_expiry_is_422(client, days):
    assert _post(client, expires_in_days=days).status_code == 422


def test_non_integer_expiry_is_422(client):
    assert _post(client, expires_in_days="soon").status_code == 422
    assert _post(client, expires_in_days=1.5).status_code == 422


def test_link_redirects_before_expiry_and_gone_after(client, monkeypatch):
    code = _post(client, expires_in_days=1).json()["code"]
    service = client.app.state.service
    base = service._clock()
    monkeypatch.setattr(service, "_clock", lambda: base + DAY - 5)
    assert client.get(f"/{code}", follow_redirects=False).status_code == 307
    monkeypatch.setattr(service, "_clock", lambda: base + DAY + 5)
    assert client.get(f"/{code}", follow_redirects=False).status_code == 410


def test_expired_link_does_not_record_clicks(client, monkeypatch):
    code = _post(client, expires_in_days=1).json()["code"]
    service = client.app.state.service
    base = service._clock()
    client.get(f"/{code}", follow_redirects=False)
    monkeypatch.setattr(service, "_clock", lambda: base + 2 * DAY)
    client.get(f"/{code}", follow_redirects=False)
    stats = client.get(f"/api/links/{code}/stats").json()
    assert stats["clicks"] == 1
    assert stats["expired"] is True


def test_stats_report_expiry(client):
    code = _post(client, expires_in_days=3).json()["code"]
    stats = client.get(f"/api/links/{code}/stats").json()
    assert stats["expires_at"] is not None
    assert stats["expired"] is False


def test_alias_and_expiry_combine(client):
    r = _post(client, alias="promo-2026", expires_in_days=7)
    assert r.status_code == 201
    assert r.json()["code"] == "promo-2026"
    assert r.json()["expires_at"] is not None
