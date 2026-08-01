"""Cloudflare AI Gateway client — ask for a tier, never a model.

Every call goes to the `tiers` gateway's universal endpoint, which takes an
ordered array of provider attempts and returns the first that succeeds. Azure
leads every tier because its credits are free; each tier falls back to a second
Azure model before it will consider anything that costs money.

No provider API key is ever sent. Provider keys live in the gateway's secret
store, so the only credential here is the gateway token.

    from aigw import chat, embed

    text = chat("low", "Classify this as spam or not: ...")
    cfg  = chat("low", prompt, json_mode=True)                 # -> parsed dict
    out  = chat("high", prompt, images=[jpeg_b64])             # vision
    vec  = embed("a sentence")                                 # 1536 dims

Environment:
    CF_ACCOUNT_ID   Cloudflare account id
    CF_AIG_TOKEN    gateway token (the only credential)
"""

from __future__ import annotations

import json
import os
import pathlib
from typing import Any

import httpx

__all__ = ["chat", "embed", "achat", "aembed", "build_chain", "TierError", "TIERS"]

_SPEC = json.loads((pathlib.Path(__file__).parent.parent / "tiers.json").read_text())

TIERS: dict[str, Any] = _SPEC["tiers"]
# The Azure resource name is deployment-specific, so it comes from the
# environment rather than the spec — nothing account-shaped lives in git.
_RESOURCE: str = os.environ.get("AZURE_RESOURCE", "")
_API_VERSION: str = _SPEC["azure_api_version"]


class TierError(RuntimeError):
    """Every attempt in the chain failed."""


def _env(*names: str) -> str:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    raise KeyError(f"set one of: {', '.join(names)}")


def _endpoint(account_id: str | None = None) -> str:
    account = account_id or _env("CF_ACCOUNT_ID", "CF_AIG_ACCOUNT_ID")
    return f"https://gateway.ai.cloudflare.com/v1/{account}/tiers"


def _headers(project: str | None, token: str | None = None) -> dict[str, str]:
    h = {
        "cf-aig-authorization": f"Bearer {token or _env('CF_AIG_TOKEN')}",
        "Content-Type": "application/json",
    }
    if project:
        h["cf-aig-metadata"] = json.dumps({"project": project})
    return h


def _openai_msgs(prompt: str, images: list[str]) -> list[dict[str, Any]]:
    if not images:
        return [{"role": "user", "content": prompt}]
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    content += [
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b}"}}
        for b in images
    ]
    return [{"role": "user", "content": content}]


def _element(
    step: dict[str, Any],
    prompt: str,
    images: list[str],
    json_mode: bool,
    temperature: float | None,
    embed_input: str | None,
    resource: str | None = None,
) -> dict[str, Any]:
    """One attempt, in whatever wire format its provider speaks.

    Never sets `max_tokens`: gpt-5.x rejects it outright, and omitting it is the
    only form that survives failover between providers with different formats.
    """
    provider, model = step["provider"], step["model"]

    if provider == "azure-openai":
        res = resource or _RESOURCE
        if not res:
            raise TierError(
                "AZURE_RESOURCE is not set — it names your Azure AI Foundry resource "
                "and every Azure step in a chain needs it. Pass resource=... if your "
                "settings loader doesn't populate os.environ."
            )
        path = step.get("path", "chat/completions")
        if path == "embeddings":
            body: dict[str, Any] = {"input": embed_input}
        else:
            body = {"messages": _openai_msgs(prompt, images)}
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            if temperature is not None:
                body["temperature"] = temperature
        return {
            "provider": provider,
            "endpoint": f"{res}/{model}/{path}?api-version={_API_VERSION}",
            "headers": {"Content-Type": "application/json"},
            "query": body,
        }

    if provider == "google-ai-studio":
        parts: list[dict[str, Any]] = [{"text": prompt}]
        parts += [
            {"inline_data": {"mime_type": "image/jpeg", "data": b}} for b in images
        ]
        gen: dict[str, Any] = {}
        if json_mode:
            gen["responseMimeType"] = "application/json"
        if temperature is not None:
            gen["temperature"] = temperature
        body = {"contents": [{"parts": parts}]}
        if gen:
            body["generationConfig"] = gen
        return {
            "provider": provider,
            "endpoint": f"v1beta/models/{model}:generateContent",
            "headers": {"Content-Type": "application/json"},
            "query": body,
        }

    if provider == "anthropic":
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        content += [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": b},
            }
            for b in images
        ]
        # Anthropic requires an explicit cap. It is the last resort and does not
        # fire while Azure credits hold.
        body = {"model": model, "max_tokens": 4096, "messages": [{"role": "user", "content": content}]}
        if temperature is not None:
            body["temperature"] = temperature
        return {
            "provider": provider,
            "endpoint": "v1/messages",
            "headers": {"Content-Type": "application/json", "anthropic-version": "2023-06-01"},
            "query": body,
        }

    # groq / cerebras / openai — OpenAI-shaped
    body = {"model": model, "messages": _openai_msgs(prompt, images)}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if temperature is not None:
        body["temperature"] = temperature
    return {
        "provider": provider,
        "endpoint": "chat/completions",
        "headers": {"Content-Type": "application/json"},
        "query": body,
    }


def build_chain(
    tier: str,
    prompt: str = "",
    *,
    images: list[str] | None = None,
    json_mode: bool = False,
    temperature: float | None = None,
    embed_input: str | None = None,
    resource: str | None = None,
) -> list[dict[str, Any]]:
    if tier not in TIERS:
        raise KeyError(f"unknown tier {tier!r}; known: {', '.join(TIERS)}")
    return [
        _element(s, prompt, images or [], json_mode, temperature, embed_input, resource)
        for s in TIERS[tier]["chain"]
    ]


def _extract_text(payload: Any) -> str:
    """Normalise whichever provider answered into plain text."""
    if isinstance(payload, dict):
        if "choices" in payload:  # azure / openai / groq / cerebras
            return payload["choices"][0]["message"]["content"] or ""
        if "candidates" in payload:  # google
            return "".join(
                p.get("text", "")
                for p in payload["candidates"][0]["content"]["parts"]
            )
        if "content" in payload:  # anthropic
            return "".join(
                b.get("text", "") for b in payload["content"] if isinstance(b, dict)
            )
    raise TierError(f"unrecognised response shape: {str(payload)[:200]}")


def _post(chain: list[dict[str, Any]], project: str | None, timeout: float,
          account_id: str | None = None, token: str | None = None) -> Any:
    r = httpx.post(_endpoint(account_id), headers=_headers(project, token),
                   json=chain, timeout=timeout)
    if r.status_code >= 400:
        raise TierError(f"every attempt failed ({r.status_code}): {r.text[:300]}")
    return r.json()


def chat(
    tier: str,
    prompt: str,
    *,
    images: list[str] | None = None,
    json_mode: bool = False,
    temperature: float | None = None,
    project: str | None = None,
    timeout: float = 180.0,
    account_id: str | None = None,
    token: str | None = None,
) -> Any:
    """Run a prompt through a tier.

    Returns the answering model's text, or the parsed object when
    ``json_mode=True``. ``images`` are base64-encoded JPEGs.
    """
    chain = build_chain(
        tier, prompt, images=images, json_mode=json_mode, temperature=temperature
    )
    text = _extract_text(_post(chain, project, timeout, account_id, token))
    if not json_mode:
        return text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # A provider that ignored json_mode may fence the object in markdown.
        s, e = text.find("{"), text.rfind("}")
        if s == -1 or e <= s:
            raise TierError(f"expected JSON, got: {text[:200]}")
        return json.loads(text[s : e + 1])


def embed(text: str, *, project: str | None = None, timeout: float = 120.0) -> list[float]:
    """Embed one string. 1536 dimensions."""
    payload = _post(build_chain("embed", embed_input=text), project, timeout)
    return payload["data"][0]["embedding"]


# --- async equivalents, for apps running inside an event loop -----------------


async def _apost(chain: list[dict[str, Any]], project: str | None, timeout: float,
                 account_id: str | None = None, token: str | None = None) -> Any:
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(_endpoint(account_id), headers=_headers(project, token), json=chain)
    if r.status_code >= 400:
        raise TierError(f"every attempt failed ({r.status_code}): {r.text[:300]}")
    return r.json()


async def achat(
    tier: str,
    prompt: str,
    *,
    images: list[str] | None = None,
    json_mode: bool = False,
    temperature: float | None = None,
    project: str | None = None,
    timeout: float = 180.0,
    account_id: str | None = None,
    token: str | None = None,
) -> Any:
    """Async :func:`chat`."""
    chain = build_chain(
        tier, prompt, images=images, json_mode=json_mode, temperature=temperature
    )
    text = _extract_text(await _apost(chain, project, timeout, account_id, token))
    if not json_mode:
        return text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        s, e = text.find("{"), text.rfind("}")
        if s == -1 or e <= s:
            raise TierError(f"expected JSON, got: {text[:200]}")
        return json.loads(text[s : e + 1])


async def aembed(text: str, *, project: str | None = None, timeout: float = 120.0) -> list[float]:
    """Async :func:`embed`."""
    payload = await _apost(build_chain("embed", embed_input=text), project, timeout)
    return payload["data"][0]["embedding"]
