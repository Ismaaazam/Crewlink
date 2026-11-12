"""
AI drafting behind a small provider abstraction.

Configuration comes ONLY from environment variables, read on the server. Nothing here
is ever sent to the browser, and no key is stored in the repository.

  AI_PROVIDER          fake (default) | anthropic
  AI_API_KEY           required for anthropic; never logged, never returned
  AI_MODEL             anthropic model id (default below; verify it for your account)
  AI_API_URL           override the API endpoint (default: Anthropic messages API)
  AI_TIMEOUT_SECONDS   per-attempt timeout (default 8)
  AI_MAX_RETRIES       retries after the first attempt (default 1)
  AI_FAKE_MODE         fake provider only: ok (default) | fail | timeout | invalid

Failure policy: a timeout, network error, HTTP error, malformed reply or a reply that
breaks the rules (e.g. push_preview over 120 characters) counts as a failed attempt.
After the retry also fails the caller gets DraftUnavailable and the UI falls back to
manual entry. Output is never silently truncated or repaired: a human approves the
exact text that will be sent, so it must be the text the provider actually produced.
"""
import json
import logging
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger("crewlink.ai")

PUSH_PREVIEW_MAX = 120
TITLE_MAX = 200
BODY_MAX = 5000
DEFAULT_MODEL = "claude-haiku-5-5"
DEFAULT_URL = "https://api.anthropic.com/v1/messages"

SYSTEM_PROMPT = (
    "You draft announcements that a union leader will review before sending to members. "
    "Reply with ONLY a JSON object with exactly these string keys: "
    '"title" (short), "body" (clear and factual, no invented details), and '
    f'"push_preview" (the notification text, at most {PUSH_PREVIEW_MAX} characters). '
    "Do not add any text outside the JSON."
)


class ProviderError(Exception):
    """One failed attempt (timeout, network, HTTP error, bad or invalid output)."""


class DraftUnavailable(Exception):
    """All attempts failed; the caller should offer manual entry."""


@dataclass(frozen=True)
class Draft:
    title: str
    body: str
    push_preview: str


def _short(text, limit):
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"


class FakeProvider:
    """Deterministic demo provider. No network, no key."""

    name = "fake"

    def __init__(self, mode=None):
        self.mode = (mode or os.environ.get("AI_FAKE_MODE", "ok")).strip().lower()

    def draft(self, prompt, timeout):
        if self.mode == "fail":
            raise ProviderError("simulated provider outage")
        if self.mode == "timeout":
            raise ProviderError("simulated provider timeout")
        if self.mode == "invalid":
            return {"title": "Too long", "body": "Body", "push_preview": "x" * (PUSH_PREVIEW_MAX + 1)}
        return {
            "title": _short(prompt, 60),
            "body": f"{prompt.strip()}\n\n(Demo draft from the fake provider. Review and edit before approving.)",
            "push_preview": _short(prompt, PUSH_PREVIEW_MAX),
        }


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key, model=DEFAULT_MODEL, url=DEFAULT_URL):
        self.api_key, self.model, self.url = api_key, model, url

    def draft(self, prompt, timeout):
        payload = json.dumps({
            "model": self.model,
            "max_tokens": 600,
            "system": SYSTEM_PROMPT,
            "messages": [{
                "role": "user",
                "content": f"Draft an announcement from these notes by the leader:\n\n{prompt}",
            }],
        }).encode()
        request = urllib.request.Request(
            self.url, data=payload, method="POST",
            headers={
                "content-type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read())
        except urllib.error.HTTPError as exc:  # before URLError: it is a subclass
            raise ProviderError(f"provider returned HTTP {exc.code}") from None
        except (TimeoutError, socket.timeout):
            raise ProviderError("provider timed out") from None
        except urllib.error.URLError as exc:
            raise ProviderError(f"network error ({type(exc.reason).__name__})") from None
        except (ValueError, OSError):
            raise ProviderError("unreadable provider response") from None
        try:
            text = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
            start, end = text.index("{"), text.rindex("}") + 1  # tolerate code fences
            return json.loads(text[start:end])
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ProviderError("provider reply was not the expected JSON") from None


def get_provider():
    name = os.environ.get("AI_PROVIDER", "fake").strip().lower()
    if name == "fake":
        return FakeProvider()
    if name == "anthropic":
        key = os.environ.get("AI_API_KEY", "").strip()
        if not key:
            raise ProviderError("AI_API_KEY is not set")
        return AnthropicProvider(
            key,
            os.environ.get("AI_MODEL", DEFAULT_MODEL),
            os.environ.get("AI_API_URL", DEFAULT_URL),
        )
    raise ProviderError(f"unknown AI_PROVIDER {name!r}")


def validate_draft(raw):
    """Server-side rules for provider output. Raises ProviderError so a bad reply is
    handled exactly like an outage (retry, then manual fallback)."""
    if isinstance(raw, Draft):
        raw = {"title": raw.title, "body": raw.body, "push_preview": raw.push_preview}
    if not isinstance(raw, dict):
        raise ProviderError("draft is not an object")
    clean = {}
    for key, limit in (("title", TITLE_MAX), ("body", BODY_MAX), ("push_preview", PUSH_PREVIEW_MAX)):
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ProviderError(f"{key} is missing or empty")
        value = value.strip()
        if len(value) > limit:
            raise ProviderError(f"{key} is {len(value)} characters; the limit is {limit}")
        clean[key] = value
    return Draft(**clean)


def _env_number(name, default, cast):
    try:
        return cast(os.environ.get(name, default))
    except (TypeError, ValueError):
        return cast(default)


def generate_draft(prompt):
    """Returns (provider_name, Draft). Raises DraftUnavailable after the retry also fails."""
    timeout = _env_number("AI_TIMEOUT_SECONDS", 8, float)
    retries = max(0, _env_number("AI_MAX_RETRIES", 1, int))
    try:
        provider = get_provider()  # a configuration problem is not transient: no retry
    except ProviderError as exc:
        logger.error("AI drafting not available: %s", exc)
        raise DraftUnavailable(str(exc)) from None
    last = None
    for attempt in range(1, retries + 2):
        try:
            return provider.name, validate_draft(provider.draft(prompt, timeout=timeout))
        except ProviderError as exc:
            last = exc
            logger.warning("AI draft attempt %d/%d failed (%s): %s", attempt, retries + 1, provider.name, exc)
    raise DraftUnavailable(str(last))
