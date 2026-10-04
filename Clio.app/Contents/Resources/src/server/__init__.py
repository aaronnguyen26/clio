"""Clio Spotlight Server and API Subsystem.

Provides REST and Server-Sent Events (SSE) interfaces for the Clio Spotlight HUD.
"""

from src.server.server import ClioServer

__all__ = ["ClioServer"]
