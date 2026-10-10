"""Cloud AI Provider Integrations (Google Gemini 2.5 Flash & Anthropic Claude 3.5 Haiku).

Path: src/ai/providers.py
Belongs to Multimodal AI Workflow Dissection Pipeline.

Implements Section 4.2 & 4.3:
- Unified Prompt & Schema Contract
- Google Gemini 2.5 Flash Provider (via Generative Language API)
- Anthropic Claude 3.5 Haiku Provider (via Anthropic Messages API)
- Deterministic Mock Provider for unit testing and offline verification

Pure Python standard library with zero external pip dependencies (uses urllib.request).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import base64
import json
import logging
import os
import re
import socket
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple, Union
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

# Standard timeout for single-pass workflow dissection
DEFAULT_API_TIMEOUT_SECONDS = 15.0

# Canonical Dissection System Prompt defining the schema contract
DISSECTION_SYSTEM_PROMPT = """You are Clio's Autonomous Desktop Workflow Dissection Engine.
Your job is to analyze the sequence of demonstrated desktop actions and summary keyframes,
then synthesize a coherent, reusable, and parameterized automation specification in JSON.

MANDATORY RULES:
1. Synthesize a concise, human-friendly 'workflow_name' (e.g. 'Send Slack Direct Message').
2. Synthesize a clean, spoken command 'canonical_trigger' (e.g. 'send slack message').
3. Synthesize a clear 'summary' explaining what the workflow does.
4. Extract dynamic parameters: Any user-typed text, recipient names, search queries, URLs, or variable numbers should be
   parameterized into the 'parameters' list with name, type, default_value, and description.
   In step payloads and descriptions, refer to these parameters using {{parameter_name}}.
5. Step Correlation:
   - Each step in 'steps' MUST include 'step_index' referencing the corresponding 1-based index from the input recorded action sequence.
   - Assign clear, high-level semantic descriptions to each step (e.g. "Focus search input", "Type message {{message_text}}").
   - For type_text or paste_text steps, 'payload' must include {"text": "{{parameter_name}}"}.
   - For press_hotkey steps, 'payload' must include {"keys": [...]}.
   - For target information, specify {"app_name": ..., "bundle_id": ..., "element_name": ...} when identifiable from context or OCR.
6. Actions must use standard primitives: launch_app, focus_app, click, double_click, right_click, drag, scroll, type_text, paste_text, press_hotkey, wait, open_url.
7. Return ONLY valid, raw JSON adhering strictly to the schema. No markdown prose or explanation outside the JSON.
"""

# JSON Schema for validation and Gemini structured output
DISSECTION_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "workflow_name": {"type": "string"},
        "canonical_trigger": {"type": "string"},
        "summary": {"type": "string"},
        "parameters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string"},
                    "default_value": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["name", "type", "default_value"],
            },
        },
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "step_index": {"type": "integer"},
                    "order": {"type": "integer"},
                    "action": {"type": "string"},
                    "description": {"type": "string"},
                    "target": {"type": "object"},
                    "payload": {"type": "object"},
                },
                "required": ["order", "action", "description"],
            },
        },
    },
    "required": ["workflow_name", "canonical_trigger", "summary", "parameters", "steps"],
}


def extract_json_payload(raw_text: str) -> Dict[str, Any]:
    """Extracts a valid JSON dict from raw LLM output, peeling markdown code fences if present."""
    clean = raw_text.strip()
    if clean.startswith("```"):
        # Match ```json ... ``` or ``` ... ```
        m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", clean)
        if m:
            clean = m.group(1).strip()
    # Try direct parse
    try:
        data = json.loads(clean)
        if isinstance(data, dict):
            return data
    except Exception:
        pass

    # Find the outermost { ... }
    first_brace = clean.find("{")
    last_brace = clean.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        sub = clean[first_brace : last_brace + 1]
        try:
            data = json.loads(sub)
            if isinstance(data, dict):
                return data
        except Exception as e:
            raise ValueError(f"Failed parsing LLM JSON output: {e} | Text excerpt: {clean[:200]}") from e

    raise ValueError(f"No valid JSON object found in response: {raw_text[:200]}")


class BaseAIProvider(ABC):
    """Abstract base class for multimodal workflow dissection providers."""

    def __init__(self, api_key: str, model: str, timeout: float = DEFAULT_API_TIMEOUT_SECONDS) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Name of the provider (e.g. 'gemini', 'claude')."""
        pass

    @abstractmethod
    def dissect_workflow(
        self,
        event_sequence: List[Dict[str, Any]],
        context_metadata: Dict[str, Any],
        initial_frame_bytes: Optional[bytes] = None,
        final_frame_bytes: Optional[bytes] = None,
    ) -> Dict[str, Any]:
        """Performs single-pass semantic dissection on the compact event sequence and summary frames."""
        pass

    @abstractmethod
    def validate_key(self) -> bool:
        """Verifies whether the configured API key is valid."""
        pass

    @abstractmethod
    def call_llm_json(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: Optional[Dict[str, Any]] = None,
        max_tokens: int = 800,
    ) -> str:
        """Invokes the LLM with structured JSON output enforcement."""
        pass



# -----------------------------------------------------------------------------
# Model Health Tracker & Circuit Breaker
# -----------------------------------------------------------------------------

class ModelHealthTracker:
    """Thread-safe circuit breaker and health tracker for AI models.

    Tracks rate limits (HTTP 429), transient outages (HTTP 503, 500, 502, 504),
    timeouts/unresponsiveness, and retired models (HTTP 404) to avoid hammering
    unhealthy models and dynamically prioritize responsive ones across both
    workflow dissection and interactive walkthrough tutoring.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cooldown_until: Dict[str, float] = {}
        self._retired_models: Set[str] = set()
        self._consecutive_failures: Dict[str, int] = {}

    def mark_success(self, model: str) -> None:
        """Records a successful call, resetting failure counts and clearing cooldown."""
        with self._lock:
            self._consecutive_failures[model] = 0
            self._cooldown_until.pop(model, None)

    def mark_rate_limited(self, model: str, cooldown_seconds: float = 60.0, base_cooldown_seconds: Optional[float] = None) -> None:
        """Mark model as rate-limited / quota exhausted (HTTP 429 or RESOURCE_EXHAUSTED).
        
        Applies exponential backoff based on consecutive failures (e.g. 60s, 120s, 240s, max 1800s)
        so alternate models are prioritized when a free plan quota is exhausted.
        """
        eff_base = base_cooldown_seconds if base_cooldown_seconds is not None else cooldown_seconds
        with self._lock:
            failures = self._consecutive_failures.get(model, 0) + 1
            self._consecutive_failures[model] = failures
            # Exponential backoff capped at 30 minutes (1800s)
            multiplier = 2 ** min(failures - 1, 5)
            cooldown = min(eff_base * multiplier, 1800.0)
            self._cooldown_until[model] = time.time() + cooldown

    def mark_unavailable(self, model: str, cooldown_seconds: float = 30.0, base_cooldown_seconds: Optional[float] = None) -> None:
        """Mark model as temporarily unavailable or unresponsive (HTTP 503/500/timeout)."""
        eff_base = base_cooldown_seconds if base_cooldown_seconds is not None else cooldown_seconds
        with self._lock:
            failures = self._consecutive_failures.get(model, 0) + 1
            self._consecutive_failures[model] = failures
            multiplier = 2 ** min(failures - 1, 4)
            cooldown = min(eff_base * multiplier, 600.0)
            self._cooldown_until[model] = time.time() + cooldown

    def mark_retired(self, model: str) -> None:
        """Mark model as permanently retired or not found (HTTP 404)."""
        with self._lock:
            self._retired_models.add(model)
            self._cooldown_until.pop(model, None)

    def is_healthy(self, model: str) -> bool:
        """Checks if a model is currently healthy and not in cooldown or retired."""
        with self._lock:
            if model in self._retired_models:
                return False
            until = self._cooldown_until.get(model, 0.0)
            return time.time() >= until

    def get_candidate_models(self, preferred_model: str, fallback_models: List[str]) -> List[str]:
        """Returns ordered list of candidate models: healthy preferred first, then healthy fallbacks, then cooling down fallbacks."""
        with self._lock:
            now = time.time()
            all_models: List[str] = []
            for m in [preferred_model] + fallback_models:
                if m and m not in all_models and m not in self._retired_models:
                    all_models.append(m)

            healthy: List[str] = []
            cooling_down: List[str] = []

            for m in all_models:
                until = self._cooldown_until.get(m, 0.0)
                if now >= until:
                    healthy.append(m)
                else:
                    cooling_down.append(m)

            cooling_down.sort(key=lambda m: self._cooldown_until.get(m, 0.0))

            if preferred_model in healthy:
                healthy.remove(preferred_model)
                healthy.insert(0, preferred_model)

            candidates = healthy + cooling_down
            return candidates or [preferred_model]

    def reset(self) -> None:
        """Clears all tracking states (useful for testing)."""
        with self._lock:
            self._cooldown_until.clear()
            self._retired_models.clear()
            self._consecutive_failures.clear()


global_model_health_tracker = ModelHealthTracker()
_preferred_gemini_model_lock = threading.Lock()
_global_preferred_gemini_model: Optional[str] = None


def is_quota_or_rate_limit_error(http_code: int, error_body: str) -> bool:
    """Detects whether an HTTP error is due to rate limits or quota exhaustion on a free or standard plan.
    
    Google Generative AI REST API returns 429 for rate limits, but can also return 400/403 with
    RESOURCE_EXHAUSTED status, quota metric errors (e.g. 'Quota exceeded for quota metric...'),
    or rate limit descriptions.
    """
    if http_code == 429:
        return True
    if http_code in (400, 403):
        body_lower = error_body.lower()
        quota_indicators = (
            "resource_exhausted",
            "quota exceeded",
            "quota_exceeded",
            "rate limit",
            "ratelimit",
            "free tier",
            "resource has been exhausted",
            "requests per minute",
            "tokens per minute",
        )
        return any(ind in body_lower for ind in quota_indicators)
    return False

DEFAULT_GEMINI_FALLBACK_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash",
    "gemini-1.5-flash-8b",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-3.5-flash",
    "gemini-3.7-flash",
    "gemini-3.8-flash",
]

DEFAULT_CLAUDE_FALLBACK_MODELS = [
    "claude-3-5-haiku-20241022",
    "claude-3-haiku-20240307",
    "claude-3-5-sonnet-20241022",
]


# -----------------------------------------------------------------------------
# Google Gemini Provider (with Multi-Model Failover & Circuit Breaker)
# -----------------------------------------------------------------------------

class GeminiProvider(BaseAIProvider):
    """Google Gemini implementation using Google Generative Language REST API.

    Defaults to gemini-3.5-flash-lite (Google's most efficient, high-RPM model),
    with automatic multi-model failover across healthy flash endpoints on
    HTTP 429 (quota exhaustion), HTTP 503 (high demand), timeouts, or retirement (HTTP 404).
    """

    def __init__(
        self,
        api_key: str,
        model: Optional[str] = None,
        timeout: float = DEFAULT_API_TIMEOUT_SECONDS,
        api_base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        fallback_models: Optional[List[str]] = None,
        health_tracker: Optional[ModelHealthTracker] = None,
    ) -> None:
        self.health_tracker = health_tracker or global_model_health_tracker
        self.fallback_models = list(fallback_models or DEFAULT_GEMINI_FALLBACK_MODELS)
        
        # If no explicit model was requested, check if a healthy model was already discovered globally
        if model:
            eff_model = model
        else:
            env_model = os.environ.get("GEMINI_MODEL")
            if env_model:
                eff_model = env_model
            else:
                with _preferred_gemini_model_lock:
                    if _global_preferred_gemini_model and self.health_tracker.is_healthy(_global_preferred_gemini_model):
                        eff_model = _global_preferred_gemini_model
                    else:
                        eff_model = "gemini-3.5-flash-lite"

        super().__init__(api_key=api_key, model=eff_model, timeout=timeout)
        self.api_base_url = api_base_url

    @property
    def provider_name(self) -> str:
        return "gemini"

    def dissect_workflow(
        self,
        event_sequence: List[Dict[str, Any]],
        context_metadata: Dict[str, Any],
        initial_frame_bytes: Optional[bytes] = None,
        final_frame_bytes: Optional[bytes] = None,
    ) -> Dict[str, Any]:
        """Dispatches single-pass payload to Gemini with JSON schema output enforcement and multi-model failover."""
        prompt_text = (
            f"{DISSECTION_SYSTEM_PROMPT}\n\n"
            f"CONTEXT METADATA:\n{json.dumps(context_metadata, indent=2)}\n\n"
            f"RECORDED EVENT SEQUENCE:\n{json.dumps(event_sequence, indent=2)}\n\n"
            f"Synthesize the WorkflowSpec according to the specified JSON schema."
        )

        parts: List[Dict[str, Any]] = [{"text": prompt_text}]

        if initial_frame_bytes:
            parts.append({
                "inline_data": {
                    "mime_type": "image/jpeg",
                    "data": base64.b64encode(initial_frame_bytes).decode("ascii"),
                }
            })

        if final_frame_bytes:
            parts.append({
                "inline_data": {
                    "mime_type": "image/jpeg",
                    "data": base64.b64encode(final_frame_bytes).decode("ascii"),
                }
            })

        req_payload = {
            "contents": [{"parts": parts}],
            "generationConfig": {
                "temperature": 0.2,
                "response_mime_type": "application/json",
                "response_schema": DISSECTION_RESPONSE_SCHEMA,
            },
        }

        data_bytes = json.dumps(req_payload).encode("utf-8")
        candidates = self.health_tracker.get_candidate_models(self.model, self.fallback_models)
        last_exception: Optional[Exception] = None
        resp_json: Optional[Dict[str, Any]] = None

        for candidate_model in candidates:
            url = f"{self.api_base_url}/models/{candidate_model}:generateContent?key={self.api_key}"
            req = urllib.request.Request(
                url,
                data=data_bytes,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                start_t = time.time()
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    resp_bytes = resp.read()
                    elapsed = time.time() - start_t
                    logger.info("Gemini API (%s) succeeded in %.2fs (HTTP %d)", candidate_model, elapsed, resp.status)
                    resp_json = json.loads(resp_bytes.decode("utf-8"))
                self.health_tracker.mark_success(candidate_model)
                self.model = candidate_model
                try:
                    from src.ai.usage_tracker import get_usage_tracker
                    get_usage_tracker().record_api_call(provider="gemini", model=candidate_model, rate_limited=False)
                except Exception:
                    pass
                with _preferred_gemini_model_lock:
                    global _global_preferred_gemini_model
                    _global_preferred_gemini_model = candidate_model
                break
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
                logger.warning("Gemini model %s HTTP %d: %s", candidate_model, e.code, err_body[:200])
                last_exception = RuntimeError(f"Gemini API HTTP {e.code} on {candidate_model}: {err_body}")
                if is_quota_or_rate_limit_error(e.code, err_body):
                    logger.warning("Gemini model %s hit rate limit/quota (free tier or RPM limit). Switching to next candidate model...", candidate_model)
                    self.health_tracker.mark_rate_limited(candidate_model)
                    try:
                        from src.ai.usage_tracker import get_usage_tracker
                        get_usage_tracker().record_api_call(
                            provider="gemini",
                            model=candidate_model,
                            rate_limited=True,
                            cooldown_seconds=60.0,
                            error_message=f"HTTP {e.code} on {candidate_model}",
                        )
                    except Exception:
                        pass
                    continue
                elif e.code in (500, 502, 503, 504, 408):
                    logger.warning("Gemini model %s temporarily unavailable (HTTP %d). Switching to next candidate model...", candidate_model, e.code)
                    self.health_tracker.mark_unavailable(candidate_model)
                    continue
                elif e.code == 404:
                    logger.warning("Gemini model %s not found (HTTP 404). Permanently retiring model...", candidate_model)
                    self.health_tracker.mark_retired(candidate_model)
                    continue
                else:
                    raise last_exception from e
            except (urllib.error.URLError, TimeoutError, socket.timeout, Exception) as e:
                logger.warning("Gemini model %s request failed/timed out: %s", candidate_model, e)
                self.health_tracker.mark_unavailable(candidate_model)
                last_exception = RuntimeError(f"Gemini request on {candidate_model} failed: {e}")
                continue
        else:
            raise last_exception or RuntimeError("All Gemini candidate models failed")

        if not resp_json:
            raise last_exception or RuntimeError("Empty Gemini response")

        # Extract generated content text
        try:
            candidates_out = resp_json.get("candidates", [])
            if not candidates_out:
                raise ValueError("Gemini returned zero candidates.")
            content = candidates_out[0].get("content", {})
            parts_out = content.get("parts", [])
            raw_text = parts_out[0].get("text", "")
            return extract_json_payload(raw_text)
        except Exception as e:
            logger.error("Failed extracting content from Gemini response: %s", e)
            raise

    def call_llm_json(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: Optional[Dict[str, Any]] = None,
        max_tokens: int = 800,
    ) -> str:
        """Invokes Gemini with structured JSON output enforcement and multi-model failover."""
        req_payload: Dict[str, Any] = {
            "systemInstruction": {
                "parts": [{"text": system_prompt}]
            },
            "contents": [
                {"role": "user", "parts": [{"text": user_prompt}]}
            ],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": max_tokens,
                "response_mime_type": "application/json",
            },
        }
        if schema:
            req_payload["generationConfig"]["response_schema"] = schema

        data_bytes = json.dumps(req_payload).encode("utf-8")
        candidates = self.health_tracker.get_candidate_models(self.model, self.fallback_models)
        last_exception: Optional[Exception] = None
        resp_json: Optional[Dict[str, Any]] = None

        for candidate_model in candidates:
            url = f"{self.api_base_url}/models/{candidate_model}:generateContent?key={self.api_key}"
            req = urllib.request.Request(
                url,
                data=data_bytes,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                start_t = time.time()
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    resp_bytes = resp.read()
                    elapsed = time.time() - start_t
                    logger.info("Gemini call_llm_json (%s) succeeded in %.2fs (HTTP %d)", candidate_model, elapsed, resp.status)
                    resp_json = json.loads(resp_bytes.decode("utf-8"))
                self.health_tracker.mark_success(candidate_model)
                self.model = candidate_model
                try:
                    from src.ai.usage_tracker import get_usage_tracker
                    get_usage_tracker().record_api_call(provider="gemini", model=candidate_model, rate_limited=False)
                except Exception:
                    pass
                with _preferred_gemini_model_lock:
                    global _global_preferred_gemini_model
                    _global_preferred_gemini_model = candidate_model
                break
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
                logger.warning("Gemini call_llm_json %s HTTP %d: %s", candidate_model, e.code, err_body[:200])
                last_exception = RuntimeError(f"Gemini API HTTP {e.code} on {candidate_model}: {err_body}")
                if is_quota_or_rate_limit_error(e.code, err_body):
                    logger.warning("Gemini model %s hit rate limit/quota (free tier or RPM limit). Switching to next candidate model...", candidate_model)
                    self.health_tracker.mark_rate_limited(candidate_model)
                    try:
                        from src.ai.usage_tracker import get_usage_tracker
                        get_usage_tracker().record_api_call(
                            provider="gemini",
                            model=candidate_model,
                            rate_limited=True,
                            cooldown_seconds=60.0,
                            error_message=f"HTTP {e.code} on {candidate_model}",
                        )
                    except Exception:
                        pass
                    continue
                elif e.code in (500, 502, 503, 504, 408):
                    logger.warning("Gemini model %s temporarily unavailable (HTTP %d). Switching to next candidate model...", candidate_model, e.code)
                    self.health_tracker.mark_unavailable(candidate_model)
                    continue
                elif e.code == 404:
                    logger.warning("Gemini model %s not found (HTTP 404). Permanently retiring model...", candidate_model)
                    self.health_tracker.mark_retired(candidate_model)
                    continue
                else:
                    raise last_exception from e
            except (urllib.error.URLError, TimeoutError, socket.timeout, Exception) as e:
                logger.warning("Gemini call_llm_json %s request failed/timed out: %s", candidate_model, e)
                self.health_tracker.mark_unavailable(candidate_model)
                last_exception = RuntimeError(f"Gemini call_llm_json on {candidate_model} failed: {e}")
                continue
        else:
            raise last_exception or RuntimeError("All Gemini candidate models failed for call_llm_json")

        if not resp_json:
            raise last_exception or RuntimeError("Empty Gemini response")

        candidates_out = resp_json.get("candidates", [])
        if not candidates_out:
            raise ValueError("Gemini returned zero candidates.")
        content = candidates_out[0].get("content", {})
        parts_out = content.get("parts", [])
        if not parts_out:
            raise ValueError("Gemini returned empty parts.")
        return parts_out[0].get("text", "")

    def validate_key(self) -> bool:
        """Validates API key via lightweight models endpoint query or candidate model probe."""
        # 1. Direct validation via the models list endpoint
        url = f"{self.api_base_url}/models?key={self.api_key}&pageSize=1"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                if resp.status == 200:
                    return True
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403):
                return False
        except Exception:
            pass

        # 2. Candidate probe across healthy models
        candidates = self.health_tracker.get_candidate_models(self.model, self.fallback_models)
        for cand in candidates:
            cand_url = f"{self.api_base_url}/models/{cand}?key={self.api_key}"
            cand_req = urllib.request.Request(cand_url, method="GET")
            try:
                with urllib.request.urlopen(cand_req, timeout=5.0) as resp:
                    if resp.status == 200:
                        return True
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    return False
                elif e.code == 404:
                    self.health_tracker.mark_retired(cand)
                    continue
            except Exception:
                continue
        return False


# -----------------------------------------------------------------------------
# Anthropic Claude Provider (with Multi-Model Failover & Circuit Breaker)
# -----------------------------------------------------------------------------

class ClaudeProvider(BaseAIProvider):
    """Anthropic Claude implementation using Anthropic Messages REST API with multi-model failover."""

    def __init__(
        self,
        api_key: str,
        model: str = "claude-3-5-haiku-20241022",
        timeout: float = DEFAULT_API_TIMEOUT_SECONDS,
        api_base_url: str = "https://api.anthropic.com/v1",
        fallback_models: Optional[List[str]] = None,
        health_tracker: Optional[ModelHealthTracker] = None,
    ) -> None:
        super().__init__(api_key=api_key, model=model, timeout=timeout)
        self.api_base_url = api_base_url
        self.fallback_models = list(fallback_models or DEFAULT_CLAUDE_FALLBACK_MODELS)
        self.health_tracker = health_tracker or global_model_health_tracker

    @property
    def provider_name(self) -> str:
        return "claude"

    def dissect_workflow(
        self,
        event_sequence: List[Dict[str, Any]],
        context_metadata: Dict[str, Any],
        initial_frame_bytes: Optional[bytes] = None,
        final_frame_bytes: Optional[bytes] = None,
    ) -> Dict[str, Any]:
        """Dispatches single-pass payload to Claude with image content blocks and multi-model failover."""
        content_blocks: List[Dict[str, Any]] = []

        if initial_frame_bytes:
            content_blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.b64encode(initial_frame_bytes).decode("ascii"),
                },
            })

        if final_frame_bytes:
            content_blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.b64encode(final_frame_bytes).decode("ascii"),
                },
            })

        user_text = (
            f"CONTEXT METADATA:\n{json.dumps(context_metadata, indent=2)}\n\n"
            f"RECORDED EVENT SEQUENCE:\n{json.dumps(event_sequence, indent=2)}\n\n"
            f"Synthesize the WorkflowSpec according to the schema contract. Output ONLY raw JSON."
        )
        content_blocks.append({"type": "text", "text": user_text})

        candidates = self.health_tracker.get_candidate_models(self.model, self.fallback_models)
        last_exception: Optional[Exception] = None
        resp_json: Optional[Dict[str, Any]] = None

        for candidate_model in candidates:
            req_payload = {
                "model": candidate_model,
                "max_tokens": 4096,
                "system": DISSECTION_SYSTEM_PROMPT,
                "messages": [
                    {"role": "user", "content": content_blocks},
                ],
                "temperature": 0.2,
            }

            url = f"{self.api_base_url}/messages"
            data_bytes = json.dumps(req_payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data_bytes,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                },
                method="POST",
            )

            try:
                start_t = time.time()
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    resp_bytes = resp.read()
                    elapsed = time.time() - start_t
                    logger.info("Claude API (%s) succeeded in %.2fs (HTTP %d)", candidate_model, elapsed, resp.status)
                    resp_json = json.loads(resp_bytes.decode("utf-8"))
                self.health_tracker.mark_success(candidate_model)
                self.model = candidate_model
                break
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
                logger.warning("Claude API model %s HTTP %d: %s", candidate_model, e.code, err_body[:200])
                last_exception = RuntimeError(f"Claude API HTTP {e.code} on {candidate_model}: {err_body}")
                if e.code == 429:
                    self.health_tracker.mark_rate_limited(candidate_model)
                    continue
                elif e.code in (500, 502, 503, 504, 529, 408):
                    self.health_tracker.mark_unavailable(candidate_model)
                    continue
                elif e.code == 404:
                    self.health_tracker.mark_retired(candidate_model)
                    continue
                else:
                    raise last_exception from e
            except (urllib.error.URLError, TimeoutError, socket.timeout, Exception) as e:
                logger.warning("Claude model %s request failed/timed out: %s", candidate_model, e)
                self.health_tracker.mark_unavailable(candidate_model)
                last_exception = RuntimeError(f"Claude request on {candidate_model} failed: {e}")
                continue
        else:
            raise last_exception or RuntimeError("All Claude candidate models failed")

        if not resp_json:
            raise last_exception or RuntimeError("Empty Claude response")

        try:
            blocks = resp_json.get("content", [])
            text_blocks = [b.get("text", "") for b in blocks if b.get("type") == "text"]
            combined = "\n".join(text_blocks)
            return extract_json_payload(combined)
        except Exception as e:
            logger.error("Failed extracting content from Claude response: %s", e)
            raise

    def validate_key(self) -> bool:
        """Validates Claude API key via a minimal 1-token test prompt across candidate models."""
        url = f"{self.api_base_url}/messages"
        candidates = self.health_tracker.get_candidate_models(self.model, self.fallback_models)

        for candidate_model in candidates:
            payload = {
                "model": candidate_model,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    if resp.status == 200:
                        return True
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    return False
                elif e.code == 404:
                    self.health_tracker.mark_retired(candidate_model)
                    continue
            except Exception as e:
                logger.debug("Claude key validation failed on %s: %s", candidate_model, e)
                continue
        return False

    def call_llm_json(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: Optional[Dict[str, Any]] = None,
        max_tokens: int = 800,
    ) -> str:
        """Invokes Claude with structured output instruction and multi-model failover."""
        prompt_with_schema = user_prompt
        if schema:
            prompt_with_schema += f"\n\nReturn strictly valid JSON conforming to this schema:\n{json.dumps(schema)}"

        candidates = self.health_tracker.get_candidate_models(self.model, self.fallback_models)
        last_exception: Optional[Exception] = None
        resp_json: Optional[Dict[str, Any]] = None

        for candidate_model in candidates:
            req_payload = {
                "model": candidate_model,
                "max_tokens": max_tokens,
                "temperature": 0.2,
                "system": system_prompt,
                "messages": [
                    {"role": "user", "content": prompt_with_schema}
                ],
            }
            url = f"{self.api_base_url}/messages"
            data_bytes = json.dumps(req_payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data_bytes,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    resp_json = json.loads(resp.read().decode("utf-8"))
                self.health_tracker.mark_success(candidate_model)
                self.model = candidate_model
                break
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
                logger.warning("Claude call_llm_json %s HTTP %d: %s", candidate_model, e.code, err_body[:200])
                last_exception = RuntimeError(f"Claude API HTTP {e.code} on {candidate_model}: {err_body}")
                if e.code == 429:
                    self.health_tracker.mark_rate_limited(candidate_model)
                    continue
                elif e.code in (500, 502, 503, 504, 529, 408):
                    self.health_tracker.mark_unavailable(candidate_model)
                    continue
                elif e.code == 404:
                    self.health_tracker.mark_retired(candidate_model)
                    continue
                else:
                    raise last_exception from e
            except (urllib.error.URLError, TimeoutError, socket.timeout, Exception) as e:
                logger.warning("Claude call_llm_json %s failed/timed out: %s", candidate_model, e)
                self.health_tracker.mark_unavailable(candidate_model)
                last_exception = RuntimeError(f"Claude call_llm_json on {candidate_model} failed: {e}")
                continue
        else:
            raise last_exception or RuntimeError("All Claude candidate models failed for call_llm_json")

        if not resp_json:
            raise last_exception or RuntimeError("Empty Claude response")

        for block in resp_json.get("content", []):
            if block.get("type") == "text":
                return block.get("text", "")
        return ""


# -----------------------------------------------------------------------------
# Mock AI Provider for Deterministic Testing & CI
# -----------------------------------------------------------------------------

class MockAIProvider(BaseAIProvider):
    """Deterministic mock provider simulating Claude / Gemini single-pass synthesis for tests."""

    def __init__(
        self,
        api_key: str = "mock-api-key-12345",
        model: str = "mock-flash-model",
        custom_response: Optional[Dict[str, Any]] = None,
        should_fail: bool = False,
    ) -> None:
        super().__init__(api_key=api_key, model=model)
        self.custom_response = custom_response
        self.should_fail = should_fail
        self.last_dissected_events: List[Dict[str, Any]] = []
        self.last_metadata: Dict[str, Any] = {}
        self.has_initial_frame = False
        self.has_final_frame = False

    @property
    def provider_name(self) -> str:
        return "mock"

    def dissect_workflow(
        self,
        event_sequence: List[Dict[str, Any]],
        context_metadata: Dict[str, Any],
        initial_frame_bytes: Optional[bytes] = None,
        final_frame_bytes: Optional[bytes] = None,
    ) -> Dict[str, Any]:
        self.last_dissected_events = event_sequence
        self.last_metadata = context_metadata
        self.has_initial_frame = initial_frame_bytes is not None and len(initial_frame_bytes) > 0
        self.has_final_frame = final_frame_bytes is not None and len(final_frame_bytes) > 0

        if self.should_fail:
            raise RuntimeError("Simulated Mock AI API failure")

        if self.custom_response:
            return dict(self.custom_response)

        # Intelligently construct a realistic dissected workflow based on events
        app_name = context_metadata.get("target_app", "Notes")
        wf_name = context_metadata.get("name") or f"Automated {app_name} Workflow"

        # Detect parameters from typed text
        params: List[Dict[str, Any]] = []
        steps: List[Dict[str, Any]] = []
        step_order = 1

        # Focus step (only prepend if not already starting with focus/launch)
        bundle_id = context_metadata.get("target_bundle_id", "com.apple.Notes")
        first_act = ""
        if event_sequence:
            first_act = str(event_sequence[0].get("action") or event_sequence[0].get("type") or "").lower()

        if first_act not in ("focus_app", "focusapp", "launch_app", "launchapp", "app_activate", "appactivate"):
            steps.append({
                "step_index": step_order,
                "order": step_order,
                "action": "focus_app",
                "description": f"Focus and activate {app_name}",
                "target": {"bundle_id": bundle_id, "app_name": app_name},
            })
            step_order += 1

        for idx, ev in enumerate(event_sequence):
            # Resolve action name from action or type
            raw_act = str(ev.get("action") or ev.get("type") or "click").lower()
            if "type_text" in raw_act or "pastetext" in raw_act:
                action = "type_text"
            elif "hotkey" in raw_act:
                action = "press_hotkey"
            elif "drag" in raw_act:
                action = "drag"
            elif "double" in raw_act:
                action = "double_click"
            elif "focus" in raw_act or "activate" in raw_act:
                action = "focus_app"
            elif "key" in raw_act:
                if ev.get("modifiers") or (ev.get("key", "").lower() in ("return", "enter", "escape", "tab")):
                    action = "press_hotkey"
                else:
                    action = "type_text"
            else:
                action = "click"

            step_ref = ev.get("step_index") or ev.get("index") or step_order
            text = ev.get("text") or ev.get("key", "")
            if action in ("type_text", "paste_text") and text:
                param_name = "input_text" if len(params) == 0 else f"input_text_{len(params) + 1}"
                params.append({
                    "name": param_name,
                    "type": "string",
                    "default_value": text,
                    "description": f"Input text for {app_name}",
                })
                steps.append({
                    "step_index": step_ref,
                    "order": step_order,
                    "action": "type_text",
                    "description": f"Type '{{{{{param_name}}}}}'",
                    "payload": {"text": f"{{{{{param_name}}}}}"},
                    "target": {"bundle_id": bundle_id},
                })
                step_order += 1
            elif action == "press_hotkey":
                keys = ev.get("keys") or ([ev.get("key")] if ev.get("key") else ["return"])
                steps.append({
                    "step_index": step_ref,
                    "order": step_order,
                    "action": "press_hotkey",
                    "description": f"Press {'+'.join(str(k) for k in keys)}",
                    "payload": {"keys": keys},
                    "target": {"bundle_id": bundle_id},
                })
                step_order += 1
            elif action == "focus_app":
                steps.append({
                    "step_index": step_ref,
                    "order": step_order,
                    "action": "focus_app",
                    "description": f"Focus and activate {app_name}",
                    "target": {"bundle_id": bundle_id, "app_name": app_name},
                })
                step_order += 1
            elif action in ("click", "double_click", "drag"):
                rec_txt = ev.get("recognized_text") or ev.get("description")
                desc = f"Click '{rec_txt}' in {app_name}" if rec_txt else (ev.get("description") or f"Click target in {app_name}")
                steps.append({
                    "step_index": step_ref,
                    "order": step_order,
                    "action": action,
                    "description": desc,
                    "target": ev.get("target", {"bundle_id": bundle_id}),
                    "coordinates": ev.get("coordinates", {}),
                })
                step_order += 1

        if not params:
            params.append({
                "name": "default_note",
                "type": "string",
                "default_value": "Meeting Notes",
                "description": "Note title or body content",
            })

        return {
            "workflow_name": wf_name,
            "canonical_trigger": wf_name.lower(),
            "summary": f"Performs demonstrated operations in {app_name}",
            "parameters": params,
            "steps": steps,
        }

    def call_llm_json(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: Optional[Dict[str, Any]] = None,
        max_tokens: int = 800,
    ) -> str:
        if self.should_fail:
            raise RuntimeError("Simulated Mock AI API failure")
        if self.custom_response:
            return json.dumps(self.custom_response)
        return json.dumps({
            "goal": "Test Walkthrough Goal",
            "summary": "Mock walkthrough synthesis",
            "target_app": "Finder",
            "steps": [
                {
                    "step_index": 1,
                    "title": "Open Finder",
                    "instruction": "Click the Finder icon in the Dock",
                    "explanation": "Activates the macOS file manager",
                    "action_type": "demonstrate_click",
                    "target_app": "Finder",
                    "target_element": {"role": "AXApplication", "title": "Finder"},
                    "fallback_screen_coords": [400.0, 300.0],
                    "spotlight_bounds": [350.0, 275.0, 100.0, 50.0],
                }
            ],
        })

    def validate_key(self) -> bool:
        return not self.should_fail


def get_ai_provider(
    provider_name: Optional[str] = None,
    api_key: Optional[str] = None,
    mock: bool = False,
    model: Optional[str] = None,
) -> Optional[BaseAIProvider]:
    """Factory helper to obtain an instantiated AI provider."""
    if mock:
        return MockAIProvider()

    from src.ai.credentials import get_credential_manager
    cred_mgr = get_credential_manager()

    p_name = provider_name
    key = api_key

    if not p_name or not key:
        detected_p, detected_k = cred_mgr.get_active_provider()
        p_name = p_name or detected_p
        key = key or detected_k

    if not p_name or not key:
        return None

    p_clean = p_name.lower().strip()
    if p_clean == "gemini":
        m = model or os.environ.get("GEMINI_MODEL")
        return GeminiProvider(api_key=key, model=m)
    elif p_clean in ("claude", "anthropic"):
        m = model or "claude-3-5-haiku-20241022"
        return ClaudeProvider(api_key=key, model=m)

    return None
