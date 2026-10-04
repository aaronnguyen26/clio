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
import time
from typing import Any, Dict, List, Optional, Tuple, Union
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
# Google Gemini Provider (gemini-2.5-flash)
# -----------------------------------------------------------------------------

class GeminiProvider(BaseAIProvider):
    """Google Gemini Flash implementation using Google Generative Language REST API.

    Defaults to gemini-3.5-flash-lite (Google's most efficient and cost-effective model),
    with automatic fallback to gemini-2.5-flash.
    """

    def __init__(
        self,
        api_key: str,
        model: Optional[str] = None,
        timeout: float = DEFAULT_API_TIMEOUT_SECONDS,
        api_base_url: str = "https://generativelanguage.googleapis.com/v1beta",
    ) -> None:
        eff_model = model or os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
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
        """Dispatches single-pass payload to Gemini with JSON schema output enforcement."""
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

        url = f"{self.api_base_url}/models/{self.model}:generateContent?key={self.api_key}"
        data_bytes = json.dumps(req_payload).encode("utf-8")
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
                logger.info("Gemini API call succeeded in %.2fs (HTTP %d)", elapsed, resp.status)
                resp_json = json.loads(resp_bytes.decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
            logger.error("Gemini API HTTP %d error: %s", e.code, err_body)
            # Auto-fallback to active flash models if model name is unrecognized or experiencing high demand (503)
            if e.code in (404, 503):
                fallback_models = [
                    "gemini-3.5-flash-lite",
                    "gemini-3.5-flash",
                    "gemini-3.7-flash",
                    "gemini-flash-lite-latest",
                    "gemini-2.5-flash",
                ]
                for fb_m in fallback_models:
                    if self.model != fb_m:
                        logger.info("Retrying with fallback model: %s", fb_m)
                        self.model = fb_m
                        return self.dissect_workflow(
                            event_sequence=event_sequence,
                            context_metadata=context_metadata,
                            initial_frame_bytes=initial_frame_bytes,
                            final_frame_bytes=final_frame_bytes,
                        )
            raise RuntimeError(f"Gemini API HTTP {e.code}: {err_body}") from e
        except Exception as e:
            logger.error("Gemini API request failed: %s", e)
            raise RuntimeError(f"Gemini API request failed: {e}") from e

        # Extract generated content text
        try:
            candidates = resp_json.get("candidates", [])
            if not candidates:
                raise ValueError("Gemini returned zero candidates.")
            content = candidates[0].get("content", {})
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
        """Invokes Gemini with structured JSON output enforcement."""
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

        url = f"{self.api_base_url}/models/{self.model}:generateContent?key={self.api_key}"
        data_bytes = json.dumps(req_payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data_bytes,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        fallback_models = [
            "gemini-3.5-flash-lite",
            "gemini-3.5-flash",
            "gemini-3.7-flash",
            "gemini-flash-lite-latest",
            "gemini-2.5-flash",
        ]
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp_bytes = resp.read()
                resp_json = json.loads(resp_bytes.decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
            logger.error("Gemini call_llm_json HTTP %d error: %s", e.code, err_body)
            if e.code in (404, 503):
                for fb_m in fallback_models:
                    if self.model != fb_m:
                        logger.info("Retrying call_llm_json with fallback model: %s", fb_m)
                        self.model = fb_m
                        return self.call_llm_json(system_prompt, user_prompt, schema, max_tokens)
            raise RuntimeError(f"Gemini API HTTP {e.code}: {err_body}") from e
        except Exception as e:
            logger.error("Gemini call_llm_json request failed: %s", e)
            raise RuntimeError(f"Gemini API request failed: {e}") from e

        candidates = resp_json.get("candidates", [])
        if not candidates:
            raise ValueError("Gemini returned zero candidates.")
        content = candidates[0].get("content", {})
        parts_out = content.get("parts", [])
        if not parts_out:
            raise ValueError("Gemini returned empty parts.")
        return parts_out[0].get("text", "")

    def validate_key(self) -> bool:
        """Validates API key via lightweight model list or test prompt."""
        url = f"{self.api_base_url}/models/{self.model}?key={self.api_key}"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                return resp.status == 200
        except Exception as e:
            logger.debug("Gemini key validation failed: %s", e)
            return False


# -----------------------------------------------------------------------------
# Anthropic Claude Provider (claude-3-5-haiku-20241022)
# -----------------------------------------------------------------------------

class ClaudeProvider(BaseAIProvider):
    """Anthropic Claude implementation using Anthropic Messages REST API."""

    def __init__(
        self,
        api_key: str,
        model: str = "claude-3-5-haiku-20241022",
        timeout: float = DEFAULT_API_TIMEOUT_SECONDS,
        api_base_url: str = "https://api.anthropic.com/v1",
    ) -> None:
        super().__init__(api_key=api_key, model=model, timeout=timeout)
        self.api_base_url = api_base_url

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
        """Dispatches single-pass payload to Claude with image content blocks and JSON schema enforcement."""
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

        req_payload = {
            "model": self.model,
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
                logger.info("Claude API call succeeded in %.2fs (HTTP %d)", elapsed, resp.status)
                resp_json = json.loads(resp_bytes.decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
            logger.error("Claude API HTTP %d error: %s", e.code, err_body)
            raise RuntimeError(f"Claude API HTTP {e.code}: {err_body}") from e
        except Exception as e:
            logger.error("Claude API request failed: %s", e)
            raise RuntimeError(f"Claude API request failed: {e}") from e

        try:
            blocks = resp_json.get("content", [])
            text_blocks = [b.get("text", "") for b in blocks if b.get("type") == "text"]
            combined = "\n".join(text_blocks)
            return extract_json_payload(combined)
        except Exception as e:
            logger.error("Failed extracting content from Claude response: %s", e)
            raise

    def validate_key(self) -> bool:
        """Validates Claude API key via a minimal 1-token test prompt."""
        url = f"{self.api_base_url}/messages"
        payload = {
            "model": self.model,
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
                return resp.status == 200
        except Exception as e:
            logger.debug("Claude key validation failed: %s", e)
            return False

    def call_llm_json(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: Optional[Dict[str, Any]] = None,
        max_tokens: int = 800,
    ) -> str:
        """Invokes Claude with structured output instruction."""
        prompt_with_schema = user_prompt
        if schema:
            prompt_with_schema += f"\n\nReturn strictly valid JSON conforming to this schema:\n{json.dumps(schema)}"

        req_payload = {
            "model": self.model,
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
                for block in resp_json.get("content", []):
                    if block.get("type") == "text":
                        return block.get("text", "")
                return ""
        except Exception as e:
            raise RuntimeError(f"Claude API request failed: {e}") from e


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
        m = model or os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
        return GeminiProvider(api_key=key, model=m)
    elif p_clean in ("claude", "anthropic"):
        m = model or "claude-3-5-haiku-20241022"
        return ClaudeProvider(api_key=key, model=m)

    return None
