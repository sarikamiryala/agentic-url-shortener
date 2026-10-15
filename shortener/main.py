"""Standalone entrypoint: `uvicorn shortener.main:app`."""
from .api import create_app

app = create_app()
