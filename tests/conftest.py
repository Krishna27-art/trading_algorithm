"""Shared test setup: give tests a strong secret so auth checks behave
the same on every machine, whether or not a real .env exists."""
import os

os.environ.setdefault("APP_SHARED_SECRET", "test-only-strong-secret-9f2c1a7e")
