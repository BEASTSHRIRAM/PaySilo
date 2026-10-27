"""Uvicorn entrypoint: ``uvicorn app.main:app --port 8000``."""

from .api import create_app

app = create_app()
