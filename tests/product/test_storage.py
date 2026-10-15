# covers: shorten, redirect, analytics
import pytest

from shortener.storage import DuplicateCode, Storage


@pytest.fixture
def storage(tmp_path):
    s = Storage(str(tmp_path / "s.db"))
    yield s
    s.close()


def test_insert_and_get(storage):
    link = storage.insert_link("abc1234", "https://example.com", created_at=100.0)
    assert link.click_count == 0
    got = storage.get_link("abc1234")
    assert got == link


def test_get_missing_returns_none(storage):
    assert storage.get_link("nope") is None


def test_duplicate_code_raises(storage):
    storage.insert_link("dup", "https://a.example")
    with pytest.raises(DuplicateCode):
        storage.insert_link("dup", "https://b.example")
    assert storage.get_link("dup").url == "https://a.example"


def test_record_click_updates_counter_and_log(storage):
    storage.insert_link("c1", "https://example.com")
    storage.record_click("c1", referrer="https://news.example", user_agent="ua", at=10.0)
    storage.record_click("c1", referrer="https://news.example", at=20.0)
    storage.record_click("c1", referrer="https://blog.example", at=30.0)
    assert storage.get_link("c1").click_count == 3
    stats = storage.click_stats("c1")
    assert stats["last_clicked_at"] == 30.0
    assert stats["top_referrers"][0] == {"referrer": "https://news.example", "clicks": 2}


def test_click_for_unknown_code_rolls_back(storage):
    with pytest.raises(Exception):
        storage.record_click("ghost")
    stats = storage.click_stats("ghost")
    assert stats["last_clicked_at"] is None


def test_sql_injection_attempt_is_inert(storage):
    storage.insert_link("safe", "https://example.com")
    assert storage.get_link("safe' OR '1'='1") is None
    assert storage.get_link("safe") is not None
