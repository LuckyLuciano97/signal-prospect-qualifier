"""Thin Anthropic wrapper, carried over from the support-triage-agent repo.

Same reasoning as there: two fixed prompt shapes, strict JSON back, no
framework in between. The API quirks this file already knows about —
``temperature`` rejected, prefill rejected in favour of
``output_config.format`` (which *guarantees* schema-conformant JSON), and
thinking sharing the ``max_tokens`` budget — cost a failed run each to learn,
so the file moves between repos intact.

Responses are cached on disk by prompt hash: re-running the pipeline while
iterating on the report costs nothing and returns identical text.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import time

import config

CACHE_DIR = config.REPO / ".llm_cache"


class LLMError(RuntimeError):
    pass


class AnthropicEngine:
    name = "anthropic"

    def __init__(self, model: str | None = None, use_cache: bool = True):
        config.load_dotenv()
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise LLMError(
                "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add "
                "a key, or run with --no-llm for rule-based scores only."
            )
        try:
            import anthropic
        except ImportError as exc:
            raise LLMError(
                "The 'anthropic' package is required. "
                "Install it with: pip install -r requirements.txt"
            ) from exc

        self.model = model or config.DEFAULT_MODEL
        self.client = anthropic.Anthropic(api_key=api_key)
        self.use_cache = use_cache
        self.calls = 0
        self.cache_hits = 0
        self.input_tokens = 0
        self.output_tokens = 0
        if use_cache:
            CACHE_DIR.mkdir(exist_ok=True)

    def _cache_path(self, key: str) -> pathlib.Path:
        return CACHE_DIR / f"{key}.json"

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 4000,
        schema: dict | None = None,
        effort: str = "low",
    ) -> str:
        """One model call. ``schema`` switches on guaranteed-JSON output."""
        payload = {
            "model": self.model,
            "system": system,
            "user": user,
            "max_tokens": max_tokens,
            "schema": schema,
            "effort": effort,
        }
        key = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()[:32]

        if self.use_cache:
            cached = self._cache_path(key)
            if cached.exists():
                self.cache_hits += 1
                return json.loads(cached.read_text(encoding="utf-8"))["text"]

        messages = [{"role": "user", "content": user}]
        output_config: dict = {"effort": effort}
        if schema:
            output_config["format"] = {"type": "json_schema", "schema": schema}

        # 529 "Overloaded" is a capacity signal, not a request problem; backoff
        # runs from 4s to a minute rather than failing a batch halfway through.
        last_error: Exception | None = None
        for attempt in range(6):
            try:
                response = self.client.messages.create(
                    model=self.model,
                    system=system,
                    messages=messages,
                    max_tokens=max_tokens,
                    output_config=output_config,
                )
                if response.stop_reason == "max_tokens":
                    raise LLMError(
                        "response hit max_tokens - thinking and answer share the "
                        "budget; raise max_tokens or lower effort"
                    )
                text = "".join(
                    block.text for block in response.content
                    if getattr(block, "type", "") == "text"
                )
                self.calls += 1
                self.input_tokens += response.usage.input_tokens
                self.output_tokens += response.usage.output_tokens
                if self.use_cache:
                    self._cache_path(key).write_text(
                        json.dumps({"text": text}), encoding="utf-8"
                    )
                return text
            except Exception as exc:  # noqa: BLE001 - retry anything transient
                # 4xx means the request itself is wrong (bad schema, bad key,
                # bad model id); retrying it six times just burns four minutes
                # per company producing the same answer.
                if type(exc).__name__ in ("BadRequestError", "AuthenticationError",
                                          "PermissionDeniedError", "NotFoundError"):
                    raise LLMError(f"non-retryable API error: {exc}") from exc
                last_error = exc
                if attempt == 5:
                    break
                delay = min(4 * (2 ** attempt), 60)
                print(f"      retry {attempt + 1}/5 in {delay}s ({type(exc).__name__})")
                time.sleep(delay)
        raise LLMError(f"Anthropic call failed after 6 attempts: {last_error}")
