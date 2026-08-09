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
    fast = chat("high", prompt, policy="fast")                 # reorder the chain
    vec  = embed("a sentence")                                 # 1536 dims

TIER vs ROUTE. A tier is a chain of INTERCHANGEABLE models — any of them
answering is acceptable and the caller may not depend on which won. A route
pins ONE model for one job and inherits its capability's failure contract. The
difference is structural, not a convention: a tier has `chain`, a route has
`model`. `embed` is a route, which is why `embed()` builds a single attempt and
`chat("embed", …)` raises.

IMAGE ROUTES ARE WORKER-ONLY, deliberately. `coloring-sheet` and `likeness-edit`
are not reachable from here, and adding them would not be a small change: image
edits are `multipart/form-data` with a binary source rather than the JSON chain
this client posts, the response is base64 image bytes rather than text, and the
per-route params and policy_model swap belong in one place. That place is
`llm-tiers-worker` (`POST /v1/image {route, prompt, image_b64?}`), which every
language in the fleet can already call over plain HTTP. Duplicating it in Python
would be a second implementation of a contract that exists to have exactly one.

Environment:
    CF_ACCOUNT_ID   Cloudflare account id
    CF_AIG_TOKEN    gateway token (the only credential)
"""

from __future__ import annotations

import json
import os
import pathlib
import warnings
from typing import Any

import httpx

__all__ = [
    "chat", "embed", "achat", "aembed", "build_chain", "build_route_element",
    "TierError", "TIERS", "ROUTES", "CAPABILITIES", "POLICIES",
]

_SPEC = json.loads((pathlib.Path(__file__).parent.parent / "tiers.json").read_text())


def _named(section: str) -> dict[str, Any]:
    """Addressable entries of a tiers.json section.

    `_`-prefixed keys are documentation — the file carries a great deal of it —
    and must never be resolvable as a tier, route or policy name.
    """
    return {k: v for k, v in (_SPEC.get(section) or {}).items() if not k.startswith("_")}


TIERS: dict[str, Any] = _named("tiers")
# A TIER is a chain of interchangeable models. A ROUTE pins ONE model for one
# job and inherits its capability's failure contract. `embed` is a route: a
# different embedding model is a different vector space, so a fallback does not
# degrade the answer, it silently poisons the index.
ROUTES: dict[str, Any] = _named("routes")
CAPABILITIES: dict[str, Any] = _named("capabilities")
POLICIES: dict[str, Any] = _named("policies")
_RETIRED_TIERS: dict[str, str] = {
    k: v for k, v in (_SPEC.get("_retired_tiers") or {}).items() if not k.startswith("_")
}
# The Azure resource name is deployment-specific, so it comes from the
# environment rather than the spec — nothing account-shaped lives in git.
_RESOURCE: str = os.environ.get("AZURE_RESOURCE", "")
_API_VERSION: str = _SPEC["azure_api_version"]

# DEPRECATED TIER NAMES, accepted for one release so an out-of-tree caller
# (or a `*_LLM_TIER` env var set in some deployed config) doesn't hard-fail on
# the rename. `bulk` was renamed to `offload` on 2026-08-08: `offload` names the
# shape of the work rather than its size, and survives the credit expiry
# unchanged. Remove this map once nothing warns.
_DEPRECATED_TIERS: dict[str, str] = {"bulk": "offload"}


def _resolve_tier(tier: str) -> str:
    """Map a deprecated tier name onto its current one, loudly."""
    if tier in TIERS:
        return tier
    current = _DEPRECATED_TIERS.get(tier)
    if current:
        warnings.warn(
            f"tier {tier!r} was renamed to {current!r}; update the call site",
            DeprecationWarning,
            stacklevel=3,
        )
        return current
    if tier in ROUTES:
        raise KeyError(
            f"{tier!r} is a ROUTE, not a tier — it pins one model and has no chain. "
            f"Reach it through its own helper (embed()), not chat()."
        )
    retired = _RETIRED_TIERS.get(tier)
    if retired:
        # A retired name gets its reason, not just "unknown" — recording the
        # retirement is pointless if the next caller has to re-derive it.
        raise KeyError(f"tier {tier!r} was retired: {retired}")
    raise KeyError(f"unknown tier {tier!r}; known: {', '.join(TIERS)}")


def _resolve_policy(policy: str | None, spec: dict[str, Any], what: str) -> str | None:
    """Validate a policy name against the declared set.

    Unknown names RAISE — the file's own rule is that a policy which quietly
    does nothing is worse than an error, because the caller believes it took
    effect. A name that is valid but INAPPLICABLE (the tier declares no
    `policy_order`) warns instead of raising: it is a real gap, but rejecting it
    would break callers that set LLM_POLICY once for every tier they touch.
    """
    if policy is None:
        return None
    if policy not in POLICIES:
        raise KeyError(f"unknown policy {policy!r}; known: {', '.join(POLICIES)}")
    if not spec.get("policy_order"):
        warnings.warn(
            f"policy {policy!r} has no effect on {what} — it declares no policy_order, "
            f"so the declared chain order is used unchanged",
            UserWarning,
            stacklevel=3,
        )
        return None
    return policy


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


def _rejects_temperature(model: str) -> bool:
    """Models that 400 on `temperature`. Azure gpt-5.x and Anthropic's sonnet/opus
    reasoning models reject it; DeepSeek, Kimi and Gemini accept it."""
    m = (model or "").lower()
    return m.startswith("gpt-5") or "sonnet" in m or "opus" in m


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
            # gpt-5.x REJECTS `temperature` (400 Unsupported parameter). Sent
            # unconditionally it does not error visibly — the gateway just moves
            # to the next link, so the caller silently gets a DIFFERENT MODEL.
            # That is precisely the stealth swap the tier system exists to
            # prevent, so only send it to models known to accept it.
            if temperature is not None and not _rejects_temperature(model):
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


def _policy_ordered(chain: list[dict[str, Any]], order: list[str] | None) -> list[dict[str, Any]]:
    """Reorder a tier's chain by a policy's model preference.

    Same rule the Worker applies: a policy only ever REORDERS the members the
    tier already declares, so it can never introduce a model the tier does not
    list. The sort is stable, so unranked members keep their measured order
    behind the ranked ones.
    """
    if not order:
        return chain
    rank = {m: i for i, m in enumerate(order)}
    return sorted(chain, key=lambda s: rank.get(s.get("model", ""), len(order)))


def build_chain(
    tier: str,
    prompt: str = "",
    *,
    images: list[str] | None = None,
    json_mode: bool = False,
    temperature: float | None = None,
    embed_input: str | None = None,
    resource: str | None = None,
    policy: str | None = None,
) -> list[dict[str, Any]]:
    tier = _resolve_tier(tier)
    spec = TIERS[tier]
    effective = _resolve_policy(policy, spec, f"tier {tier!r}")
    order = (spec.get("policy_order") or {}).get(effective) if effective else None
    return [
        _element(s, prompt, images or [], json_mode, temperature, embed_input, resource)
        for s in _policy_ordered(spec["chain"], order)
    ]


def build_route_element(
    route: str,
    *,
    embed_input: str | None = None,
    prompt: str = "",
    images: list[str] | None = None,
    json_mode: bool = False,
    temperature: float | None = None,
    resource: str | None = None,
) -> dict[str, Any]:
    """Build the SINGLE attempt a route resolves to.

    A route is one model, so this returns one element rather than a chain, and
    it refuses a route that has grown a `chain` key — that shape is the exact
    mistake moving `embed` out of `tiers` was meant to make impossible.
    """
    spec = ROUTES.get(route)
    if spec is None:
        raise KeyError(f"unknown route {route!r}; known: {', '.join(ROUTES)}")
    if "chain" in spec:
        raise TierError(
            f"route {route!r} has a chain — a route pins exactly one model. "
            f"Its capability contract is "
            f"{CAPABILITIES.get(spec.get('capability', ''), {}).get('failover', 'unspecified')!r}."
        )
    step = {
        "provider": spec.get("provider", "azure-openai"),
        "model": spec["model"],
        **({"path": spec["path"]} if spec.get("path") else {}),
    }
    return _element(step, prompt, images or [], json_mode, temperature, embed_input, resource)


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
    policy: str | None = None,
) -> Any:
    """Run a prompt through a tier.

    Returns the answering model's text, or the parsed object when
    ``json_mode=True``. ``images`` are base64-encoded JPEGs.

    ``policy`` (``free`` | ``fast`` | ``cheap`` | ``best``) reorders the tier's
    chain via its declared ``policy_order``. It can never introduce a model the
    tier does not already list, so capability stays a property of the tier.
    Omitting it uses the declared order, which IS ``free``. An unknown policy
    name RAISES rather than being ignored — a knob that silently does nothing is
    worse than an error. This is the one thing an environment should override
    (``LLM_POLICY=free`` locally, ``fast`` in production).
    """
    chain = build_chain(
        tier, prompt, images=images, json_mode=json_mode, temperature=temperature,
        policy=policy,
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
    """Embed one string. 1536 dimensions.

    Resolves the ``embed`` ROUTE, not a tier: exactly one model, no failover.
    Its capability contract is ``same-model-different-provider-only`` — a
    different embedding model is a different vector space, so a silent fallback
    writes geometrically meaningless vectors into an existing index, returns
    200, and is only recoverable by re-embedding everything.
    """
    payload = _post([build_route_element("embed", embed_input=text)], project, timeout)
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
    policy: str | None = None,
) -> Any:
    """Async :func:`chat`."""
    chain = build_chain(
        tier, prompt, images=images, json_mode=json_mode, temperature=temperature,
        policy=policy,
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
    payload = await _apost([build_route_element("embed", embed_input=text)], project, timeout)
    return payload["data"][0]["embedding"]
