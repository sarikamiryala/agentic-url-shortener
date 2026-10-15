# covers: link_safety
"""Blocklist behaviour at link creation."""
import pytest
from fastapi.testclient import TestClient

from shortener.api import create_app
from shortener.config import Settings
from shortener.safety import Blocklist

MATCH_MODE = "@@MATCH_MODE@@"


def _post(client, url):
    return client.post("/api/links", json={"url": url})


def test_blocklisted_destination_is_rejected_at_creation(client):
    r = _post(client, "https://malware.example/payload")
    assert r.status_code == 422
    assert r.json()["detail"] == "destination is blocked"


def test_unrelated_destination_is_still_allowed(client):
    assert _post(client, "https://example.com/ok").status_code == 201


def test_matching_ignores_case_and_trailing_dot(client):
    assert _post(client, "https://MALWARE.example./x").status_code == 422


def test_subdomain_handling_follows_the_configured_mode(client):
    status = _post(client, "https://cdn.malware.example/x").status_code
    assert status == (422 if MATCH_MODE == "host_and_subdomains" else 201)


def test_lookalike_suffix_is_not_treated_as_a_subdomain(client):
    assert _post(client, "https://notmalware.example/x").status_code == 201


def test_extra_entries_from_settings(tmp_path):
    settings = Settings(db_path=str(tmp_path / "a.db"), blocklist_extra=("bad.test",))
    app = create_app(settings)
    with TestClient(app) as c:
        assert _post(c, "https://bad.test/x").status_code == 422
    app.state.storage.close()


def test_blocklist_file_is_loaded(tmp_path):
    f = tmp_path / "block.txt"
    f.write_text("# comment\n\nfile-bad.test\n")
    settings = Settings(db_path=str(tmp_path / "b.db"), blocklist_file=str(f))
    app = create_app(settings)
    with TestClient(app) as c:
        assert _post(c, "https://file-bad.test/x").status_code == 422
    app.state.storage.close()


def test_missing_blocklist_file_fails_fast(tmp_path):
    settings = Settings(db_path=str(tmp_path / "c.db"), blocklist_file=str(tmp_path / "nope.txt"))
    with pytest.raises(FileNotFoundError):
        create_app(settings)


def test_blocklist_unit():
    bl = Blocklist({"Evil.Test"}, "exact_host")
    assert bl.is_blocked("https://evil.test/a")
    assert not bl.is_blocked("https://sub.evil.test/a")
    assert Blocklist({"evil.test"}, "host_and_subdomains").is_blocked("https://a.b.evil.test/")
    with pytest.raises(ValueError):
        Blocklist({"x"}, "regex")
