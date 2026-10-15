# covers: link_safety
"""Redirect-time enforcement: links created before a domain was blocklisted must stop working."""
from fastapi.testclient import TestClient

from shortener.api import create_app
from shortener.config import Settings
from shortener.safety import Blocklist


def test_link_blocklisted_after_creation_stops_redirecting(tmp_path):
    app = create_app(Settings(db_path=str(tmp_path / "r.db"), rate_limit_per_minute=1000))
    with TestClient(app) as c:
        code = c.post("/api/links", json={"url": "https://later-bad.test/x"}).json()["code"]
        assert c.get(f"/{code}", follow_redirects=False).status_code == 307
        app.state.service._blocklist = Blocklist({"later-bad.test"})
        blocked = c.get(f"/{code}", follow_redirects=False)
        assert blocked.status_code == 403
        assert blocked.json()["detail"] == "destination is blocked"
        assert c.get(f"/api/links/{code}/stats").json()["clicks"] == 1
    app.state.storage.close()


def test_unaffected_links_keep_redirecting(tmp_path):
    app = create_app(Settings(db_path=str(tmp_path / "r2.db"), rate_limit_per_minute=1000))
    with TestClient(app) as c:
        code = c.post("/api/links", json={"url": "https://example.com/fine"}).json()["code"]
        app.state.service._blocklist = Blocklist({"later-bad.test"})
        assert c.get(f"/{code}", follow_redirects=False).status_code == 307
    app.state.storage.close()
