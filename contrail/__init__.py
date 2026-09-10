"""Contrail -- a trace store and analysis layer for Claude agent runs."""

from .models import Run, Span
from .store import Store

__version__ = "0.1.0"
__all__ = ["Run", "Span", "Store", "__version__"]
