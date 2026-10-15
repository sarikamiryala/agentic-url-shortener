# covers: shorten, redirect, analytics, validation, rate_limit
from fastapi.testclient import TestClient

from shortener.api import create_app
from shortener.config import Settings


def _create(client, url="https://example.com/page"):
    return client.post("/api/links", json={"url": url})


def test_create_returns_201_with_short_url(client):
    r = _create(client)
    assert r.status_code == 201
    body = r.json()
    assert body["url"] == "https://example.com/page"
    assert body["short_url"] == f"http://short.test/{body['code']}"


def test_create_rejects_invalid_url_with_422(client):
    r = _create(client, "javascript:alert(1)")
    assert r.status_code == 422
    assert "http" in r.json()["detail"]


def test_create_rejects_missing_body_field(client):
    assert client.post("/api/links", json={}).status_code == 422


def test_redirect_is_307_with_location(client):
    code = _create(client).json()["code"]
    r = client.get(f"/{code}", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "https://example.com/page"


def test_redirect_unknown_is_404(client):
    assert client.get("/doesnotexist", follow_redirects=False).status_code == 404


def test_redirect_rejects_weird_codes(client):
    assert client.get("/bad.code!", follow_redirects=False).status_code == 404


def test_stats_count_clicks_and_referrers(client):
    code = _create(client).json()["code"]
    for _ in range(2):
        client.get(f"/{code}", headers={"referer": "https://news.example"}, follow_redirects=False)
    client.get(f"/{code}", follow_redirects=False)
    stats = client.get(f"/api/links/{code}/stats").json()
    assert stats["clicks"] == 3
    assert stats["last_clicked_at"] is not None
    assert stats["top_referrers"] == [{"referrer": "https://news.example", "clicks": 2}]


def test_stats_unknown_is_404(client):
    assert client.get("/api/links/nope/stats").status_code == 404


def test_health(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_rate_limit_returns_429_with_retry_after(tmp_path):
    settings = Settings(db_path=str(tmp_path / "rl.db"), rate_limit_per_minute=3)
    app = create_app(settings)
    with TestClient(app) as c:
        codes = [c.post("/api/links", json={"url": "https://example.com"}).status_code for _ in range(4)]
        last = c.post("/api/links", json={"url": "https://example.com"})
    app.state.storage.close()
    assert codes == [201, 201, 201, 429]
    assert last.status_code == 429
    assert int(last.headers["retry-after"]) >= 1


def test_rate_limit_does_not_block_redirects(tmp_path):
    settings = Settings(db_path=str(tmp_path / "rl2.db"), rate_limit_per_minute=1)
    app = create_app(settings)
    with TestClient(app) as c:
        code = c.post("/api/links", json={"url": "https://example.com"}).json()["code"]
        statuses = [c.get(f"/{code}", follow_redirects=False).status_code for _ in range(5)]
    app.state.storage.close()
    assert statuses == [307] * 5
