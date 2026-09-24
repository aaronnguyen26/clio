"""Actuator Factory with Runtime Environment Autodetection.

Belongs to FEAT-ACT-10 (ActuatorFactory).
Instantiates live MacOSActuator on Darwin desktop or MockActuator in CI/test/headless environments.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Optional, Union

from src.actuators.base import BaseActuator
from src.actuators.mock import MockActuator
from src.actuators.types import (
    ActuatorError,
    ActuatorMode,
)

logger = logging.getLogger(__name__)


class ActuatorFactory:
    """Factory responsible for instantiating the appropriate desktop actuator."""

    @staticmethod
    def is_macos() -> bool:
        """Returns True if the current operating system is macOS (Darwin)."""
        return sys.platform == "darwin"

    @staticmethod
    def is_ci() -> bool:
        """Returns True if executing inside a CI runner (e.g. GitHub Actions)."""
        ci_env = os.environ.get("CI", "").strip().lower()
        gha_env = os.environ.get("GITHUB_ACTIONS", "").strip().lower()
        return ci_env in ("true", "1", "yes") or gha_env in ("true", "1", "yes")

    @classmethod
    def detect_environment(cls) -> str:
        """Returns a string describing the detected execution environment."""
        env_override = os.environ.get("TASK_AUTOMATOR_ACTUATOR", "").strip().lower()
        if env_override:
            return f"env_override({env_override})"
        if cls.is_ci():
            return "ci_headless"
        if cls.is_macos():
            return "darwin_native"
        return f"unsupported_os({sys.platform})"

    @classmethod
    def create(
        cls,
        mode: Union[str, ActuatorMode] = ActuatorMode.AUTO,
        fallback_to_mock: bool = True,
        **kwargs: Any,
    ) -> BaseActuator:
        """Creates and returns an actuator instance based on mode and environment.

        Resolution hierarchy:
        1. Environment variable `TASK_AUTOMATOR_ACTUATOR` (e.g. 'mock' or 'macos').
        2. Explicit `mode` parameter ('mock', 'macos', 'auto', 'live', 'test').
        3. CI environment check (`CI=true` -> MockActuator).
        4. Platform check (`sys.platform == 'darwin'` -> MacOSActuator, else MockActuator).

        Args:
            mode: Explicit mode ('auto', 'mock', 'macos', 'live', 'test') or ActuatorMode enum.
            fallback_to_mock: If True, falls back to MockActuator if MacOSActuator fails to initialize.
            **kwargs: Extra parameters forwarded to actuator constructor.

        Returns:
            An instance conforming to BaseActuator.

        Raises:
            ActuatorError: If live macOS actuator requested on non-Darwin without fallback.
        """
        # Convert string to enum if needed
        if isinstance(mode, str):
            clean_mode = mode.lower().strip()
            if clean_mode in ("live", "macos"):
                resolved_mode = ActuatorMode.MACOS
            elif clean_mode in ("mock", "test"):
                resolved_mode = ActuatorMode.MOCK
            elif clean_mode == "auto":
                resolved_mode = ActuatorMode.AUTO
            else:
                try:
                    resolved_mode = ActuatorMode(clean_mode)
                except ValueError:
                    raise ActuatorError(f"Unknown actuator mode: {mode!r}. Valid: {list(ActuatorMode)}")
        else:
            resolved_mode = mode

        # Check environment variable override
        env_override = os.environ.get("TASK_AUTOMATOR_ACTUATOR", "").strip().lower()
        if env_override in ("mock", "test"):
            logger.info("Instantiating MockActuator via TASK_AUTOMATOR_ACTUATOR override.")
            return MockActuator(**kwargs)
        elif env_override in ("macos", "live"):
            resolved_mode = ActuatorMode.MACOS

        # Resolve mode
        if resolved_mode == ActuatorMode.MOCK:
            return MockActuator(**kwargs)

        if resolved_mode == ActuatorMode.AUTO:
            if cls.is_ci():
                logger.info("CI environment detected; using MockActuator.")
                return MockActuator(**kwargs)
            if not cls.is_macos():
                logger.info("Non-macOS platform (%s) detected; using MockActuator.", sys.platform)
                return MockActuator(**kwargs)
            # On macOS outside CI, attempt to use MacOSActuator
            resolved_mode = ActuatorMode.MACOS

        # Attempt to load MacOSActuator
        if resolved_mode == ActuatorMode.MACOS:
            if not cls.is_macos():
                if fallback_to_mock:
                    logger.warning("MacOSActuator requested on non-macOS (%s); falling back to MockActuator.", sys.platform)
                    return MockActuator(**kwargs)
                raise ActuatorError(f"Cannot instantiate MacOSActuator on non-macOS platform '{sys.platform}'")

            try:
                from src.actuators.macos import MacOSActuator
                return MacOSActuator(**kwargs)
            except Exception as e:
                if fallback_to_mock:
                    logger.warning(
                        "Failed to initialize MacOSActuator (%s); falling back to MockActuator.",
                        e,
                    )
                    return MockActuator(**kwargs)
                raise ActuatorError(f"Failed to initialize MacOSActuator: {e}") from e

        return MockActuator(**kwargs)


def get_actuator(
    mode: Union[str, ActuatorMode] = ActuatorMode.AUTO,
    **kwargs: Any,
) -> BaseActuator:
    """Convenience entry point for creating an actuator."""
    return ActuatorFactory.create(mode=mode, **kwargs)
