import pytest
from fastapi.testclient import TestClient

from shortener.api import create_app
from shortener.config import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(
        db_path=str(tmp_path / "test.db"),
        base_url="http://short.test",
        rate_limit_per_minute=1000,
    )


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app) as c:
        yield c
    app.state.storage.close()
