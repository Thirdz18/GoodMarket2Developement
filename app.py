"""Primary Flask application module.

This thin module exposes `app` for WSGI/ASGI servers while legacy code
continues to bootstrap through main.py.
"""

from main import app  # noqa: F401
