# covers: validation
import pytest

from shortener.validation import InvalidURL, validate_url


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://example.com/path?q=1#frag",
        "https://sub.example.co.uk:8443/a/b",
        "  https://example.com/trimmed  ",
    ],
)
def test_accepts_valid_urls(url):
    assert validate_url(url) == url.strip()


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "example.com",
        "ftp://example.com/file",
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "https://",
        "https://user:pass@example.com",
        "https://example.com/has space",
        "https://example.com:notaport/",
        "http://localhost/admin",
        "http://foo.localhost/",
        "http://127.0.0.1:8000/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/",
    ],
)
def test_rejects_bad_urls(url):
    with pytest.raises(InvalidURL):
        validate_url(url)


def test_rejects_non_string():
    with pytest.raises(InvalidURL):
        validate_url(None)


def test_rejects_over_length():
    with pytest.raises(InvalidURL):
        validate_url("https://example.com/" + "a" * 2100)
    assert validate_url("https://example.com/" + "a" * 20, max_length=100)
