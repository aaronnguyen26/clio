"""API Reconnection and Key Validation Utilities for Walkthrough Subsystem.

Path: src/walkthrough/cloud_ai/api_reconnect.py
Provides seamless transition from local-only execution back to full Cloud AI orchestration
once the user inputs an API key.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.ai.credentials import get_credential_manager
from src.walkthrough.cloud_ai.cloud_gateway import CloudAIWalkthroughGateway

logger = logging.getLogger(__name__)


def is_cloud_api_available() -> bool:
    """Checks whether an active API key is present in environment or credentials store."""
    cm = get_credential_manager()
    gemini_key = cm.get_api_key("gemini")
    claude_key = cm.get_api_key("claude") or cm.get_api_key("anthropic")
    return bool(gemini_key or claude_key)


def get_cloud_provider_status() -> Dict[str, Any]:
    """Returns the current readiness and provider status for cloud AI walkthroughs."""
    cm = get_credential_manager()
    gemini_key = cm.get_api_key("gemini")
    claude_key = cm.get_api_key("claude") or cm.get_api_key("anthropic")

    available = bool(gemini_key or claude_key)
    active = "Gemini" if gemini_key else ("Claude" if claude_key else None)

    return {
        "cloud_available": available,
        "gemini_key_detected": bool(gemini_key),
        "claude_key_detected": bool(claude_key),
        "active_provider": active,
    }


def reconnect_cloud_ai(api_key: str, provider_name: str = "gemini") -> CloudAIWalkthroughGateway:
    """Configures a new API key, updates credentials store, and initializes a CloudAIWalkthroughGateway."""
    cm = get_credential_manager()
    cm.set_api_key(provider_name.lower(), api_key)

    prov_lower = provider_name.lower()
    if prov_lower == "gemini":
        from src.ai.providers import GeminiProvider
        provider = GeminiProvider(api_key=api_key)
    elif prov_lower in ("claude", "anthropic"):
        from src.ai.providers import ClaudeProvider
        provider = ClaudeProvider(api_key=api_key)
    else:
        from src.ai.providers import get_ai_provider
        provider = get_ai_provider()

    gateway = CloudAIWalkthroughGateway(provider=provider)
    logger.info("Successfully reconnected Cloud AI Walkthrough Gateway with provider %s", provider_name)
    return gateway
