"""Secure API Credential Management for Multimodal AI Dissection.

Path: src/ai/credentials.py
Belongs to Multimodal AI Workflow Dissection Pipeline.

Implements the fallback lookup hierarchy specified in Section 4.1:
1. Environment variables (GEMINI_API_KEY, ANTHROPIC_API_KEY).
2. macOS System Keychain item via Security.framework / /usr/bin/security CLI
   (Service: com.clio.desktop.credentials).
3. Clio configuration file (~/.task_automator/config.json or ~/.clio/config.json).

Zero external dependencies: pure Python standard library.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

KEYCHAIN_SERVICE = "com.clio.desktop.credentials"

PROVIDER_ENV_VARS: Dict[str, List[str]] = {
    "gemini": ["GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API"],
    "claude": ["ANTHROPIC_API_KEY", "CLAUDE_API_KEY"],
    "anthropic": ["ANTHROPIC_API_KEY", "CLAUDE_API_KEY"],
}

KEYCHAIN_ACCOUNTS: Dict[str, str] = {
    "gemini": "gemini_api_key",
    "claude": "claude_api_key",
    "anthropic": "claude_api_key",
}

CONFIG_FILE_PATHS: List[Path] = [
    Path.home() / ".trio" / "config.json",
    Path.home() / ".task_automator" / "config.json",
    Path.home() / ".clio" / "config.json",
]


def _load_dotenv_if_present() -> None:
    """Lightweight .env parser without external dependencies."""
    candidates = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parent.parent.parent / ".env",
        Path.home() / ".trio" / ".env",
        Path.home() / ".clio" / ".env",
    ]
    for p in candidates:
        if p.exists() and p.is_file():
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        if k and k not in os.environ:
                            os.environ[k] = v
            except Exception:
                pass


class CredentialManager:
    """Manages secure retrieval and persistence of AI provider API keys."""

    def __init__(
        self,
        service: str = KEYCHAIN_SERVICE,
        custom_config_path: Optional[Path] = None,
        use_keychain: bool = True,
        load_dotenv: bool = True,
    ) -> None:
        self.service = service
        self.custom_config_path = custom_config_path
        self._use_keychain = use_keychain and (sys.platform == "darwin")
        if load_dotenv and not custom_config_path:
            _load_dotenv_if_present()

    def get_api_key(self, provider: str) -> Optional[str]:
        """Resolves an API key for the requested provider following the 3-tier hierarchy.

        Hierarchy:
        1. Environment variables
        2. macOS Keychain
        3. Config file
        """
        p_norm = provider.lower().strip()

        # 1. Environment variables
        env_vars = PROVIDER_ENV_VARS.get(p_norm, [f"{p_norm.upper()}_API_KEY"])
        for env_var in env_vars:
            val = os.environ.get(env_var, "").strip()
            if val:
                return val

        # 2. macOS System Keychain
        if self._use_keychain:
            account = KEYCHAIN_ACCOUNTS.get(p_norm, f"{p_norm}_api_key")
            keychain_val = self._read_keychain(account)
            if keychain_val:
                return keychain_val

        # 3. Clio Configuration file
        config_val = self._read_config_file(p_norm)
        if config_val:
            return config_val

        return None

    def set_api_key(
        self,
        provider: str,
        api_key: str,
        persist_keychain: bool = True,
        persist_config: bool = False,
    ) -> bool:
        """Stores an API key in Keychain and/or configuration file."""
        p_norm = provider.lower().strip()
        key_clean = api_key.strip()
        if not key_clean:
            return False

        # Temporarily set in current process environment
        env_vars = PROVIDER_ENV_VARS.get(p_norm, [f"{p_norm.upper()}_API_KEY"])
        for env_var in env_vars:
            os.environ[env_var] = key_clean

        success = True
        if persist_keychain and self._use_keychain:
            account = KEYCHAIN_ACCOUNTS.get(p_norm, f"{p_norm}_api_key")
            keychain_success = self._write_keychain(account, key_clean)
            if not keychain_success:
                success = False

        if persist_config:
            cfg_success = self._write_config_file(p_norm, key_clean)
            if not cfg_success:
                success = False

        return success

    def delete_api_key(self, provider: str) -> bool:
        """Removes an API key from environment, Keychain, and configuration files."""
        p_norm = provider.lower().strip()

        # Clear env vars
        env_vars = PROVIDER_ENV_VARS.get(p_norm, [f"{p_norm.upper()}_API_KEY"])
        for env_var in env_vars:
            if env_var in os.environ:
                del os.environ[env_var]

        # Clear Keychain
        if self._use_keychain:
            account = KEYCHAIN_ACCOUNTS.get(p_norm, f"{p_norm}_api_key")
            self._delete_keychain(account)

        # Clear config file
        self._delete_config_file(p_norm)
        return True

    def get_active_provider(self) -> Tuple[Optional[str], Optional[str]]:
        """Returns the primary active provider and its API key.

        Priority order: Gemini (fastest/cheapest) -> Claude -> None.
        Returns:
            (provider_name, api_key) or (None, None).
        """
        for prov in ("gemini", "claude"):
            key = self.get_api_key(prov)
            if key:
                return prov, key
        return None, None

    def get_configured_providers(self) -> Dict[str, Dict[str, Any]]:
        """Returns status of providers without exposing plain API keys."""
        status: Dict[str, Dict[str, Any]] = {}
        for prov in ("gemini", "claude"):
            key = self.get_api_key(prov)
            has_key = bool(key)
            masked = f"{key[:4]}...{key[-4:]}" if (key and len(key) >= 12) else ("***" if key else None)
            
            # Determine source
            source = "none"
            if key:
                env_vars = PROVIDER_ENV_VARS.get(prov, [])
                if any(os.environ.get(ev) == key for ev in env_vars):
                    source = "environment"
                elif self._use_keychain and self._read_keychain(KEYCHAIN_ACCOUNTS.get(prov, "")) == key:
                    source = "keychain"
                else:
                    source = "config_file"

            status[prov] = {
                "configured": has_key,
                "masked_key": masked,
                "source": source,
                "model_default": os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite") if prov == "gemini" else "claude-3-5-haiku-20241022",
            }
        return status

    # -------------------------------------------------------------------------
    # macOS Keychain Helpers
    # -------------------------------------------------------------------------

    def _read_keychain(self, account: str) -> Optional[str]:
        if not self._use_keychain:
            return None
        services = [self.service]
        if self.service == "com.trio.desktop.credentials":
            services.append("com.clio.desktop.credentials")
        elif self.service == "com.clio.desktop.credentials":
            services.append("com.trio.desktop.credentials")

        for s in services:
            try:
                cmd = [
                    "/usr/bin/security",
                    "find-generic-password",
                    "-s", s,
                    "-a", account,
                    "-w",
                ]
                res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=3.0)
                if res.returncode == 0:
                    val = res.stdout.strip()
                    if val:
                        return val
            except Exception as e:
                logger.debug("Keychain read failed for service %s account %s: %s", s, account, e)
        return None

    def _write_keychain(self, account: str, secret: str) -> bool:
        if not self._use_keychain:
            return False
        try:
            cmd = [
                "/usr/bin/security",
                "add-generic-password",
                "-U",
                "-s", self.service,
                "-a", account,
                "-w", secret,
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=3.0)
            return res.returncode == 0
        except Exception as e:
            logger.debug("Keychain write failed for account %s: %s", account, e)
            return False

    def _delete_keychain(self, account: str) -> bool:
        if not self._use_keychain:
            return False
        try:
            cmd = [
                "/usr/bin/security",
                "delete-generic-password",
                "-s", self.service,
                "-a", account,
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=3.0)
            return res.returncode == 0
        except Exception as e:
            logger.debug("Keychain delete failed for account %s: %s", account, e)
            return False

    # -------------------------------------------------------------------------
    # Configuration File Helpers
    # -------------------------------------------------------------------------

    def _get_config_paths(self) -> List[Path]:
        if self.custom_config_path:
            return [self.custom_config_path]
        return CONFIG_FILE_PATHS

    def _read_config_file(self, provider: str) -> Optional[str]:
        for cfg_path in self._get_config_paths():
            if cfg_path.exists():
                try:
                    data = json.loads(cfg_path.read_text(encoding="utf-8"))
                    keys = data.get("api_keys", {})
                    if provider in keys:
                        val = str(keys[provider]).strip()
                        if val:
                            return val
                    # Also check flat keys
                    flat_key = f"{provider}_api_key"
                    if flat_key in data:
                        val = str(data[flat_key]).strip()
                        if val:
                            return val
                except Exception as e:
                    logger.debug("Failed reading config at %s: %s", cfg_path, e)
        return None

    def _write_config_file(self, provider: str, api_key: str) -> bool:
        target_path = self.custom_config_path or CONFIG_FILE_PATHS[0]
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            data: Dict[str, Any] = {}
            if target_path.exists():
                try:
                    data = json.loads(target_path.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
            if "api_keys" not in data or not isinstance(data["api_keys"], dict):
                data["api_keys"] = {}
            data["api_keys"][provider] = api_key
            target_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            try:
                target_path.chmod(0o600)
            except Exception:
                pass
            return True
        except Exception as e:
            logger.warning("Failed writing API key to config file %s: %s", target_path, e)
            return False

    def _delete_config_file(self, provider: str) -> bool:
        for cfg_path in self._get_config_paths():
            if cfg_path.exists():
                try:
                    data = json.loads(cfg_path.read_text(encoding="utf-8"))
                    modified = False
                    if "api_keys" in data and isinstance(data["api_keys"], dict) and provider in data["api_keys"]:
                        del data["api_keys"][provider]
                        modified = True
                    flat_key = f"{provider}_api_key"
                    if flat_key in data:
                        del data[flat_key]
                        modified = True
                    if modified:
                        cfg_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
                except Exception:
                    pass
        return True


# Global default instance
_default_credential_manager: Optional[CredentialManager] = None


def get_credential_manager() -> CredentialManager:
    global _default_credential_manager
    if _default_credential_manager is None:
        _default_credential_manager = CredentialManager()
    return _default_credential_manager
