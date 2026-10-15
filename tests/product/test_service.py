# covers: shorten, redirect, analytics
import pytest

from shortener.config import Settings
from shortener.service import ALPHABET, LinkNotFound, ShortenerService
from shortener.storage import Storage
from shortener.validation import InvalidURL


@pytest.fixture
def service(tmp_path):
    storage = Storage(str(tmp_path / "svc.db"))
    svc = ShortenerService(storage, Settings(db_path="unused"), clock=lambda: 1000.0)
    yield svc
    storage.close()


def test_create_returns_code_of_configured_length(service):
    link = service.create("https://example.com/a")
    assert len(link.code) == 7
    assert set(link.code) <= set(ALPHABET)
    assert link.created_at == 1000.0


def test_create_rejects_invalid_url(service):
    with pytest.raises(InvalidURL):
        service.create("javascript:alert(1)")


def test_codes_are_unique_across_many_links(service):
    codes = {service.create(f"https://example.com/{i}").code for i in range(200)}
    assert len(codes) == 200


def test_resolve_records_click(service):
    link = service.create("https://example.com/x")
    assert service.resolve(link.code, referrer="https://r.example") == "https://example.com/x"
    stats = service.stats(link.code)
    assert stats.clicks == 1
    assert stats.top_referrers == [{"referrer": "https://r.example", "clicks": 1}]


def test_resolve_unknown_raises(service):
    with pytest.raises(LinkNotFound):
        service.resolve("missing")


def test_stats_unknown_raises(service):
    with pytest.raises(LinkNotFound):
        service.stats("missing")
