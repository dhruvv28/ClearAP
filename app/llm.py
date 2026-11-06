"""One small wrapper over the two supported model providers.

Gemini is used when GEMINI_API_KEY is set (Google AI Studio has a free tier),
otherwise Claude when ANTHROPIC_API_KEY is set, otherwise no AI at all and the
rule-based parser runs alone. Callers never need to know which one ran.
"""
import httpx

from . import config


def provider() -> str | None:
    if config.GEMINI_API_KEY:
        return "gemini"
    if config.ANTHROPIC_API_KEY:
        return "anthropic"
    return None


def model_name() -> str | None:
    return {"gemini": config.GEMINI_MODEL, "anthropic": config.ANTHROPIC_MODEL}.get(provider())


def display_name() -> str | None:
    return {"gemini": "Gemini", "anthropic": "Claude"}.get(provider())


def available() -> bool:
    return provider() is not None


RETRYABLE = {429, 500, 502, 503, 504}


def friendly_error(e: Exception) -> str:
    """Short, human-readable reason for the run log (no URLs or stack traces)."""
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        return {400: "rejected the request (400)", 401: "rejected the API key (401)",
                403: "didn't allow this API key (403)", 404: "couldn't find the model (404)",
                429: "hit its rate limit (429)", 500: "had a server error (500)",
                503: "was busy (503)"}.get(code, f"returned an error ({code})")
    if isinstance(e, httpx.TimeoutException):
        return "timed out"
    if isinstance(e, httpx.TransportError):
        return "couldn't be reached"
    return f"failed ({type(e).__name__})"


last_model: str | None = None   # which model answered the most recent call (runs are serialised)


def _models() -> list[str]:
    if provider() == "gemini":
        models = [config.GEMINI_MODEL]
        if config.GEMINI_FALLBACK_MODEL and config.GEMINI_FALLBACK_MODEL != config.GEMINI_MODEL:
            models.append(config.GEMINI_FALLBACK_MODEL)
        return models
    return [config.ANTHROPIC_MODEL]


def complete(prompt: str, image_png_b64: str | None = None, max_tokens: int = 2000,
             json_mode: bool = False, timeout: float | None = None, retries: int = 1) -> str:
    """Send one prompt and return the reply.

    - Rate-limited (429): quotas are per model, so move straight to the fallback model.
    - Busy / server error / timeout: wait briefly and retry the same model once.
    - Anything else (bad key, bad request): raise immediately.
    """
    import time
    global last_model
    models = _models()
    last_err: Exception | None = None
    for i, model in enumerate(models):
        for attempt in range(retries + 1):
            try:
                out = _complete_once(prompt, image_png_b64, max_tokens, json_mode, timeout, model)
                last_model = model
                return out
            except httpx.HTTPStatusError as e:
                last_err = e
                code = e.response.status_code
                if code == 429:
                    break                      # this model's quota is used up: try the next model
                if code not in RETRYABLE:
                    raise
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_err = e
            if attempt < retries:
                time.sleep(1.5)
        else:
            continue  # retries exhausted on a busy model: try the next one too
    raise last_err or RuntimeError("No AI provider configured")


def _complete_once(prompt: str, image_png_b64: str | None, max_tokens: int, json_mode: bool,
                   timeout: float | None, model: str | None = None) -> str:
    timeout = timeout or config.LLM_TIMEOUT_S
    p = provider()
    if p == "gemini":
        parts = []
        if image_png_b64:
            parts.append({"inline_data": {"mime_type": "image/png", "data": image_png_b64}})
        parts.append({"text": prompt})
        gen = {"maxOutputTokens": max_tokens}
        if json_mode:
            gen["responseMimeType"] = "application/json"
        r = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model or config.GEMINI_MODEL}:generateContent",
            headers={"x-goog-api-key": config.GEMINI_API_KEY, "content-type": "application/json"},
            json={"contents": [{"role": "user", "parts": parts}], "generationConfig": gen},
            timeout=timeout,
        )
        r.raise_for_status()
        cands = r.json().get("candidates") or []
        if not cands:
            raise ValueError("Gemini returned no candidates")
        return "".join(part.get("text", "") for part in cands[0].get("content", {}).get("parts", []))

    if p == "anthropic":
        content = []
        if image_png_b64:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image_png_b64}})
        content.append({"type": "text", "text": prompt})
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers=config.anthropic_headers(),
            json={"model": model or config.ANTHROPIC_MODEL, "max_tokens": max_tokens,
                  "messages": [{"role": "user", "content": content}]},
            timeout=timeout,
        )
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")

    raise RuntimeError("No AI provider configured")
