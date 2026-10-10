"""Application Usage Limit & Gemini Free-Tier Quota Tracker for Clio.

Tracks and enforces app-level feature usage limits grounded in the Gemini Free API
Key tier (15 RPM, 250 RPD), translated directly into how many times the user can
use the Walkthrough feature and the Automation (workflow execution & dissection)
feature per day and per minute.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Default Gemini Free-Tier & App Feature Quotas
DEFAULT_WALKTHROUGH_DAILY_LIMIT = 25
DEFAULT_WALKTHROUGH_RPM_LIMIT = 5
DEFAULT_AUTOMATION_DAILY_LIMIT = 25
DEFAULT_AUTOMATION_RPM_LIMIT = 5
DEFAULT_GEMINI_RPD_LIMIT = 250
DEFAULT_GEMINI_RPM_LIMIT = 15


class AppUsageLimitTracker:
    """Thread-safe tracker and enforcer for Walkthrough and Automation usage limits."""

    def __init__(
        self,
        storage_path: Optional[Path] = None,
        in_memory: bool = False,
        walkthrough_daily_limit: int = DEFAULT_WALKTHROUGH_DAILY_LIMIT,
        walkthrough_rpm_limit: int = DEFAULT_WALKTHROUGH_RPM_LIMIT,
        automation_daily_limit: int = DEFAULT_AUTOMATION_DAILY_LIMIT,
        automation_rpm_limit: int = DEFAULT_AUTOMATION_RPM_LIMIT,
        gemini_rpd_limit: int = DEFAULT_GEMINI_RPD_LIMIT,
        gemini_rpm_limit: int = DEFAULT_GEMINI_RPM_LIMIT,
    ) -> None:
        self._lock = threading.RLock()
        self._in_memory = in_memory
        if storage_path is not None:
            self._storage_path = Path(storage_path)
        else:
            self._storage_path = Path.home() / ".clio" / "usage_limits.json"

        # Limits configuration
        self.walkthrough_daily_limit = max(1, int(walkthrough_daily_limit))
        self.walkthrough_rpm_limit = max(1, int(walkthrough_rpm_limit))
        self.automation_daily_limit = max(1, int(automation_daily_limit))
        self.automation_rpm_limit = max(1, int(automation_rpm_limit))
        self.gemini_rpd_limit = max(1, int(gemini_rpd_limit))
        self.gemini_rpm_limit = max(1, int(gemini_rpm_limit))

        # Daily counters (keyed by YYYY-MM-DD local date)
        self._current_day: str = self._today_str()
        self.walkthrough_used_today: int = 0
        self.walkthrough_cloud_used_today: int = 0
        self.walkthrough_local_used_today: int = 0

        self.automation_used_today: int = 0
        self.automation_cloud_used_today: int = 0
        self.automation_local_used_today: int = 0

        self.api_calls_today: int = 0
        self.active_model: str = "gemini-2.5-flash"
        self.last_blocked_feature: Optional[str] = None
        self.last_blocked_reason: Optional[str] = None

        # Sliding 60-second window timestamps
        self._walkthrough_timestamps: List[float] = []
        self._automation_timestamps: List[float] = []
        self._api_call_timestamps: List[float] = []

        # Rate-limit cooldown tracking (from HTTP 429 or burst exhaustion)
        self._rate_limit_until: float = 0.0
        self._last_429_error: Optional[str] = None

        if not self._in_memory:
            self._load()

    @staticmethod
    def _today_str(now: Optional[float] = None) -> str:
        dt = datetime.fromtimestamp(now if now is not None else time.time())
        return dt.strftime("%Y-%m-%d")

    @staticmethod
    def _seconds_until_daily_reset(now: Optional[float] = None) -> int:
        ts = now if now is not None else time.time()
        dt = datetime.fromtimestamp(ts)
        tomorrow = (dt + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return max(1, int((tomorrow - dt).total_seconds()))

    @staticmethod
    def _format_reset_countdown(seconds: int) -> str:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        if hours > 0:
            return f"{hours:02d}H {minutes:02d}M"
        secs = seconds % 60
        return f"{minutes:02d}M {secs:02d}S"

    def _rollover_if_needed(self, now: Optional[float] = None) -> None:
        ts = now if now is not None else time.time()
        today = self._today_str(ts)
        if today != self._current_day:
            self._current_day = today
            self.walkthrough_used_today = 0
            self.walkthrough_cloud_used_today = 0
            self.walkthrough_local_used_today = 0
            self.automation_used_today = 0
            self.automation_cloud_used_today = 0
            self.automation_local_used_today = 0
            self.api_calls_today = 0
            self.last_blocked_feature = None
            self.last_blocked_reason = None

        cutoff = ts - 60.0
        self._walkthrough_timestamps = [
            t for t in self._walkthrough_timestamps if t > cutoff
        ]
        self._automation_timestamps = [
            t for t in self._automation_timestamps if t > cutoff
        ]
        self._api_call_timestamps = [
            t for t in self._api_call_timestamps if t > cutoff
        ]

    def _load(self) -> None:
        with self._lock:
            try:
                if not self._storage_path.exists():
                    return
                raw = json.loads(self._storage_path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    return
                self.walkthrough_daily_limit = max(
                    1,
                    int(
                        raw.get(
                            "walkthrough_daily_limit",
                            DEFAULT_WALKTHROUGH_DAILY_LIMIT,
                        )
                    ),
                )
                self.walkthrough_rpm_limit = max(
                    1,
                    int(
                        raw.get(
                            "walkthrough_rpm_limit",
                            DEFAULT_WALKTHROUGH_RPM_LIMIT,
                        )
                    ),
                )
                self.automation_daily_limit = max(
                    1,
                    int(
                        raw.get(
                            "automation_daily_limit",
                            DEFAULT_AUTOMATION_DAILY_LIMIT,
                        )
                    ),
                )
                self.automation_rpm_limit = max(
                    1,
                    int(
                        raw.get(
                            "automation_rpm_limit",
                            DEFAULT_AUTOMATION_RPM_LIMIT,
                        )
                    ),
                )
                self.gemini_rpd_limit = max(
                    1, int(raw.get("gemini_rpd_limit", DEFAULT_GEMINI_RPD_LIMIT))
                )
                self.gemini_rpm_limit = max(
                    1, int(raw.get("gemini_rpm_limit", DEFAULT_GEMINI_RPM_LIMIT))
                )

                saved_day = str(raw.get("day", ""))
                today = self._today_str()
                if saved_day == today:
                    self._current_day = saved_day
                    self.walkthrough_used_today = max(
                        0, int(raw.get("walkthrough_used_today", 0))
                    )
                    self.walkthrough_cloud_used_today = max(
                        0, int(raw.get("walkthrough_cloud_used_today", 0))
                    )
                    self.walkthrough_local_used_today = max(
                        0, int(raw.get("walkthrough_local_used_today", 0))
                    )
                    self.automation_used_today = max(
                        0, int(raw.get("automation_used_today", 0))
                    )
                    self.automation_cloud_used_today = max(
                        0, int(raw.get("automation_cloud_used_today", 0))
                    )
                    self.automation_local_used_today = max(
                        0, int(raw.get("automation_local_used_today", 0))
                    )
                    self.api_calls_today = max(
                        0, int(raw.get("api_calls_today", 0))
                    )
                if raw.get("active_model"):
                    self.active_model = str(raw["active_model"])
            except Exception as e:
                logger.warning("Failed to load usage limits from %s: %s", self._storage_path, e)

    def _save(self) -> None:
        if self._in_memory:
            return
        with self._lock:
            try:
                self._storage_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                payload = {
                    "day": self._current_day,
                    "walkthrough_daily_limit": self.walkthrough_daily_limit,
                    "walkthrough_rpm_limit": self.walkthrough_rpm_limit,
                    "walkthrough_used_today": self.walkthrough_used_today,
                    "walkthrough_cloud_used_today": self.walkthrough_cloud_used_today,
                    "walkthrough_local_used_today": self.walkthrough_local_used_today,
                    "automation_daily_limit": self.automation_daily_limit,
                    "automation_rpm_limit": self.automation_rpm_limit,
                    "automation_used_today": self.automation_used_today,
                    "automation_cloud_used_today": self.automation_cloud_used_today,
                    "automation_local_used_today": self.automation_local_used_today,
                    "gemini_rpd_limit": self.gemini_rpd_limit,
                    "gemini_rpm_limit": self.gemini_rpm_limit,
                    "api_calls_today": self.api_calls_today,
                    "active_model": self.active_model,
                    "updated_at": time.time(),
                }
                tmp_path = self._storage_path.with_suffix(".tmp")
                tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                os.replace(tmp_path, self._storage_path)
            except Exception as e:
                logger.debug("Could not persist usage limits to %s: %s", self._storage_path, e)

    def can_use(
        self, feature: str, now: Optional[float] = None
    ) -> Tuple[bool, Optional[str]]:
        """Checks whether 'walkthrough' or 'automation' can be executed within current limits."""
        ts = now if now is not None else time.time()
        norm = feature.strip().lower()
        with self._lock:
            self._rollover_if_needed(ts)
            reset_str = self._format_reset_countdown(
                self._seconds_until_daily_reset(ts)
            )

            if norm == "walkthrough":
                if self.walkthrough_used_today >= self.walkthrough_daily_limit:
                    reason = (
                        f"Daily Walkthrough limit reached ({self.walkthrough_used_today}/{self.walkthrough_daily_limit} "
                        f"Gemini Free-Tier uses today). Resets in {reset_str}."
                    )
                    self.last_blocked_feature = "walkthrough"
                    self.last_blocked_reason = reason
                    return False, reason
                if len(self._walkthrough_timestamps) >= self.walkthrough_rpm_limit:
                    oldest = min(self._walkthrough_timestamps)
                    wait_s = max(1, int(60.0 - (ts - oldest)))
                    reason = (
                        f"Walkthrough per-minute burst limit reached ({len(self._walkthrough_timestamps)}/{self.walkthrough_rpm_limit} RPM). "
                        f"Please wait {wait_s}s."
                    )
                    self.last_blocked_feature = "walkthrough"
                    self.last_blocked_reason = reason
                    return False, reason
                return True, None

            if norm == "automation":
                if self.automation_used_today >= self.automation_daily_limit:
                    reason = (
                        f"Daily Automation limit reached ({self.automation_used_today}/{self.automation_daily_limit} "
                        f"Gemini Free-Tier uses today). Resets in {reset_str}."
                    )
                    self.last_blocked_feature = "automation"
                    self.last_blocked_reason = reason
                    return False, reason
                if len(self._automation_timestamps) >= self.automation_rpm_limit:
                    oldest = min(self._automation_timestamps)
                    wait_s = max(1, int(60.0 - (ts - oldest)))
                    reason = (
                        f"Automation per-minute burst limit reached ({len(self._automation_timestamps)}/{self.automation_rpm_limit} RPM). "
                        f"Please wait {wait_s}s."
                    )
                    self.last_blocked_feature = "automation"
                    self.last_blocked_reason = reason
                    return False, reason
                return True, None

            return True, None

    def consume(
        self,
        feature: str,
        is_cloud_ai: bool = False,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Records 1 usage of 'walkthrough' or 'automation' and returns updated status."""
        ts = now if now is not None else time.time()
        norm = feature.strip().lower()
        with self._lock:
            self._rollover_if_needed(ts)
            if norm == "walkthrough":
                self.walkthrough_used_today += 1
                if is_cloud_ai:
                    self.walkthrough_cloud_used_today += 1
                else:
                    self.walkthrough_local_used_today += 1
                self._walkthrough_timestamps.append(ts)
                if self.last_blocked_feature == "walkthrough":
                    self.last_blocked_feature = None
                    self.last_blocked_reason = None
            elif norm == "automation":
                self.automation_used_today += 1
                if is_cloud_ai:
                    self.automation_cloud_used_today += 1
                else:
                    self.automation_local_used_today += 1
                self._automation_timestamps.append(ts)
                if self.last_blocked_feature == "automation":
                    self.last_blocked_feature = None
                    self.last_blocked_reason = None
            self._save()
            return self.get_status(now=ts)

    def record_api_call(
        self,
        provider: str = "gemini",
        model: Optional[str] = None,
        rate_limited: bool = False,
        cooldown_seconds: float = 0.0,
        error_message: Optional[str] = None,
        now: Optional[float] = None,
    ) -> None:
        """Records an underlying Gemini API request and synchronizes rate-limit state."""
        ts = now if now is not None else time.time()
        with self._lock:
            self._rollover_if_needed(ts)
            if model:
                self.active_model = str(model)
            self.api_calls_today += 1
            self._api_call_timestamps.append(ts)
            if rate_limited:
                self._rate_limit_until = max(
                    self._rate_limit_until, ts + max(1.0, float(cooldown_seconds))
                )
                self._last_429_error = error_message or "Gemini Free-Tier 429 rate limit reached"
            self._save()

    def update_limits(
        self,
        walkthrough_daily_limit: Optional[int] = None,
        automation_daily_limit: Optional[int] = None,
        walkthrough_rpm_limit: Optional[int] = None,
        automation_rpm_limit: Optional[int] = None,
        gemini_rpd_limit: Optional[int] = None,
        gemini_rpm_limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Updates feature usage limits and persists them."""
        with self._lock:
            if walkthrough_daily_limit is not None:
                self.walkthrough_daily_limit = max(1, min(1000, int(walkthrough_daily_limit)))
            if automation_daily_limit is not None:
                self.automation_daily_limit = max(1, min(1000, int(automation_daily_limit)))
            if walkthrough_rpm_limit is not None:
                self.walkthrough_rpm_limit = max(1, min(100, int(walkthrough_rpm_limit)))
            if automation_rpm_limit is not None:
                self.automation_rpm_limit = max(1, min(100, int(automation_rpm_limit)))
            if gemini_rpd_limit is not None:
                self.gemini_rpd_limit = max(1, min(10000, int(gemini_rpd_limit)))
            if gemini_rpm_limit is not None:
                self.gemini_rpm_limit = max(1, min(1000, int(gemini_rpm_limit)))
            self.last_blocked_feature = None
            self.last_blocked_reason = None
            self._save()
            return self.get_status()

    def reset_usage(self, feature: Optional[str] = None) -> Dict[str, Any]:
        """Resets daily and per-minute counters for 'walkthrough', 'automation', or all."""
        with self._lock:
            norm = (feature or "all").strip().lower()
            if norm in ("all", "walkthrough"):
                self.walkthrough_used_today = 0
                self.walkthrough_cloud_used_today = 0
                self.walkthrough_local_used_today = 0
                self._walkthrough_timestamps.clear()
            if norm in ("all", "automation"):
                self.automation_used_today = 0
                self.automation_cloud_used_today = 0
                self.automation_local_used_today = 0
                self._automation_timestamps.clear()
            if norm == "all":
                self.api_calls_today = 0
                self._api_call_timestamps.clear()
                self._rate_limit_until = 0.0
                self._last_429_error = None
            self.last_blocked_feature = None
            self.last_blocked_reason = None
            self._save()
            return self.get_status()

    def get_status(self, now: Optional[float] = None) -> Dict[str, Any]:
        """Returns comprehensive usage quota telemetry for UI and API consumers."""
        ts = now if now is not None else time.time()
        with self._lock:
            self._rollover_if_needed(ts)

            # Check global_model_health_tracker for any live 429 cooldowns
            cooldown_sec = max(0.0, self._rate_limit_until - ts)
            try:
                from src.ai.providers import global_model_health_tracker
                for _m_name, until_ts in list(global_model_health_tracker._cooldown_until.items()):
                    if until_ts > ts:
                        cooldown_sec = max(cooldown_sec, until_ts - ts)
            except Exception:
                pass

            wt_rem = max(0, self.walkthrough_daily_limit - self.walkthrough_used_today)
            wt_rpm_used = len(self._walkthrough_timestamps)
            wt_rpm_rem = max(0, self.walkthrough_rpm_limit - wt_rpm_used)
            wt_pct_rem = round(
                (wt_rem / float(max(1, self.walkthrough_daily_limit))) * 100.0, 1
            )

            auto_rem = max(0, self.automation_daily_limit - self.automation_used_today)
            auto_rpm_used = len(self._automation_timestamps)
            auto_rpm_rem = max(0, self.automation_rpm_limit - auto_rpm_used)
            auto_pct_rem = round(
                (auto_rem / float(max(1, self.automation_daily_limit))) * 100.0, 1
            )

            api_rpm_used = len(self._api_call_timestamps)
            api_rpm_rem = max(0, self.gemini_rpm_limit - api_rpm_used)
            api_rpd_rem = max(0, self.gemini_rpd_limit - self.api_calls_today)

            reset_secs = self._seconds_until_daily_reset(ts)
            reset_fmt = self._format_reset_countdown(reset_secs)

            is_wt_exhausted = wt_rem <= 0 or wt_rpm_rem <= 0
            is_auto_exhausted = auto_rem <= 0 or auto_rpm_rem <= 0
            is_rate_limited = cooldown_sec > 0.0 or api_rpm_rem <= 0

            health_status = "NOMINAL"
            if is_rate_limited:
                health_status = "RATE_LIMITED"
            elif is_wt_exhausted or is_auto_exhausted:
                health_status = "QUOTA_REACHED"
            elif wt_rem <= 5 or auto_rem <= 5:
                health_status = "LOW_QUOTA"

            return {
                "day": self._current_day,
                "tier": "gemini_free_tier",
                "tier_name": "Gemini Free Tier",
                "tier_badge": f"GEMINI FREE KEY • {self.gemini_rpm_limit} RPM / {self.walkthrough_daily_limit} DAILY",
                "health_status": health_status,
                "reset_in_seconds": reset_secs,
                "reset_formatted": reset_fmt,
                "walkthrough": {
                    "used": self.walkthrough_used_today,
                    "limit": self.walkthrough_daily_limit,
                    "daily_limit": self.walkthrough_daily_limit,
                    "remaining": wt_rem,
                    "percent_remaining": wt_pct_rem,
                    "rpm_used": wt_rpm_used,
                    "rpm_limit": self.walkthrough_rpm_limit,
                    "rpm_remaining": wt_rpm_rem,
                    "cloud_ai_used": self.walkthrough_cloud_used_today,
                    "local_used": self.walkthrough_local_used_today,
                    "exhausted": is_wt_exhausted,
                },
                "automation": {
                    "used": self.automation_used_today,
                    "limit": self.automation_daily_limit,
                    "daily_limit": self.automation_daily_limit,
                    "remaining": auto_rem,
                    "percent_remaining": auto_pct_rem,
                    "rpm_used": auto_rpm_used,
                    "rpm_limit": self.automation_rpm_limit,
                    "rpm_remaining": auto_rpm_rem,
                    "cloud_ai_used": self.automation_cloud_used_today,
                    "local_used": self.automation_local_used_today,
                    "exhausted": is_auto_exhausted,
                },
                "gemini_api": {
                    "active_model": self.active_model,
                    "calls_today": self.api_calls_today,
                    "daily_limit": self.gemini_rpd_limit,
                    "daily_remaining": api_rpd_rem,
                    "rpm_used": api_rpm_used,
                    "rpm_limit": self.gemini_rpm_limit,
                    "rpm_remaining": api_rpm_rem,
                    "rate_limited": is_rate_limited,
                    "cooldown_seconds": int(round(cooldown_sec)),
                    "last_429_error": self._last_429_error,
                },
                "limit_reached": bool(is_wt_exhausted or is_auto_exhausted),
                "last_blocked_feature": self.last_blocked_feature,
                "last_blocked_reason": self.last_blocked_reason,
                "timestamp": ts,
            }

    def get_snapshot(self, now: Optional[float] = None) -> Dict[str, Any]:
        """Alias for get_status()."""
        return self.get_status(now=now)


_global_usage_tracker: Optional[AppUsageLimitTracker] = None
_tracker_lock = threading.Lock()


def get_usage_tracker() -> AppUsageLimitTracker:
    """Returns the singleton AppUsageLimitTracker instance."""
    global _global_usage_tracker
    with _tracker_lock:
        if _global_usage_tracker is None:
            _global_usage_tracker = AppUsageLimitTracker()
        return _global_usage_tracker
