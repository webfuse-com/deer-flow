"""[argus patch #174] Run tiers: the ceiling a restricted run cannot widen.

A run tier says who the answer is for. ``owner`` (no tier) and ``dm`` are the
stack owner talking to their own Atlas and keep today's behaviour. ``public``
(a Slack channel thread any colleague in the channel can read and steer) and,
later, ``private`` are restricted: their ceiling comes from a policy file the
platform bakes into the image, never from anything a citizen can edit.

The policy file
---------------

``argus-tier-policy.json`` at :data:`TIER_POLICY_PATH`, the image's
site-packages, next to the overlay modules (the same root
``app/gateway/routers/tools_proxy.py`` reads ``http-exposed-tools.json``
from). It is NOT read from ``config.yaml``, the agent config, an environment
variable or a mounted volume: the gateway Quadlet and every config file live in
the citizen's fork, so each of those is citizen-editable. The acropolis overlay
ships the file; a policy change is an image rebuild (fleet-wide, reviewed in
acropolis).

Schema (version 1)::

    {
      "version": 1,
      "tier_only_tools": ["slack_channel_history", "slack_search"],
      "tiers": {
        "public": {
          "agent": "atlas-slack-public",
          "memory_mode": "off",
          "subagents": false,
          "skills": [],
          "pythia_ring_max": "internal",
          "strip_context": ["user_email"],
          "tools": ["slack_channel_history", "pythia_query", "web_search"],
          "mcp_servers": {"pythia": "http://argus-kb-api:8000/pythia/mcp"},
          "disable_clarification": true,
          "silence_token": null,
          "prompt": "Platform text appended after the agent's soul."
        }
      }
    }

Fail closed: a missing, unreadable or invalid file, an unknown ``version``, or
a missing or invalid tier entry means :func:`policy_for` returns ``None`` and
every consumer refuses that tier (the relay refuses the event, run admission
refuses the run, assembly fails the build, the MCP interceptor refuses the
call). Nothing falls back to the owner's full agent.

Enforcement points (each one alone holds the ceiling):

- ``app/channels/slack.py`` ``relay_event``: the policy's agent, never
  ``tier_agents`` and never the channel default.
- ``app/channels/manager.py`` ``_resolve_run_params`` and
  ``app/gateway/services.py`` ``start_run``: :func:`apply_tier_policy` is the
  last write to both run-config sections, so ``session.context`` from
  ``config.yaml`` cannot override it; admission also binds the tier to the
  thread (:data:`ARGUS_TIER_KEY` in server-owned thread metadata), so any later
  run on that thread (web UI, Chronos, atlas-mcp) gets the same ceiling.
- ``agents/lead_agent/agent.py``: memory, subagents, bootstrap and skills off;
  the tool list filtered by :func:`tier_tool_allowed` after every append path;
  a final bound-tool assertion; a call-time guard
  (``TierToolGuardMiddleware``); the Pythia ring clamped.
- ``mcp/run_tier.py``: ``X-Argus-Run-Tier`` on every MCP call, set by the last
  interceptor, and a refusal of any server URL outside the policy.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from collections.abc import Iterable, Mapping, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# Hard-coded on purpose: an env var or a config key would be citizen-controlled.
TIER_POLICY_PATH = Path("/app/backend/.venv/lib/python3.12/site-packages/argus-tier-policy.json")
TIER_POLICY_VERSION = 1

# runtime.context key and server-owned thread-metadata key.
ARGUS_TIER_KEY = "argus_tier"
# The per-call MCP header (mcp/run_tier.py).
RUN_TIER_HEADER = "X-Argus-Run-Tier"
# Header value for a run that names no tier.
OWNER_TIER = "owner"
# Tiers that are the owner talking to their own Atlas: no policy, no restriction.
UNRESTRICTED_TIERS = frozenset({OWNER_TIER, "dm"})
# Tiers a policy may define.
POLICY_TIERS = frozenset({"public", "private"})
# Strictest wins when two carriers name different tiers. An unknown tier ranks
# above everything so it is the one chosen, and then refused (no policy).
_TIER_RANK = {OWNER_TIER: 0, "dm": 0, "private": 1, "public": 2}
_UNKNOWN_TIER_RANK = 3

# Removed from every run-config carrier of a tiered run, whatever the policy says.
# user_email mints a personal Pythia caller token; allowed_tools is a schedule's
# list that would otherwise be unioned into a ceiling.
ALWAYS_STRIPPED_CONTEXT_KEYS = ("user_email", "allowed_tools")

MEMORY_MODES = frozenset({"off", "read-only"})
PYTHIA_RINGS = ("none", "external", "internal", "hierarchical", "personal")
_AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9-]+$")
_TIER_VALUE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class TierPolicyUnavailable(RuntimeError):
    """A restricted tier was asked for and no valid policy covers it."""


@dataclass(frozen=True)
class TierPolicy:
    tier: str
    agent: str
    tools: frozenset[str]
    mcp_servers: Mapping[str, str]  # server key -> normalized URL
    memory_mode: str = "off"
    skills: frozenset[str] = frozenset()
    pythia_ring_max: str = "internal"
    strip_context: tuple[str, ...] = ()
    disable_clarification: bool = True
    silence_token: str | None = None
    prompt: str | None = None
    subagents: bool = False
    tier_only_tools: frozenset[str] = frozenset()

    @property
    def mcp_urls(self) -> frozenset[str]:
        return frozenset(self.mcp_servers.values())


@dataclass(frozen=True)
class TierPolicies:
    version: int
    sha256: str
    tiers: Mapping[str, TierPolicy] = field(default_factory=dict)
    tier_only_tools: frozenset[str] = frozenset()


_cache_lock = threading.Lock()
_cache: tuple[Any, TierPolicies | None] | None = None


def normalize_mcp_url(url: object) -> str | None:
    """Comparable form of an MCP server URL, or None for a non-HTTP URL.

    Scheme and host are lower-cased and a trailing ``/`` dropped; userinfo,
    port, path and query stay as written, so ``?x=1`` or ``user@`` makes a
    different URL (never on the allowlist).
    """
    if not isinstance(url, str) or not url.strip():
        return None
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return None
    path = parts.path.rstrip("/")
    normalized = f"{parts.scheme.lower()}://{parts.netloc.lower()}{path}"
    if parts.query:
        normalized = f"{normalized}?{parts.query}"
    return normalized


def _string_list(value: object, field_name: str, tier: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"tiers.{tier}.{field_name} must be a list of non-empty strings")
    return value


def _parse_tier(tier: str, raw: object, tier_only_tools: frozenset[str]) -> TierPolicy:
    if not isinstance(raw, dict):
        raise ValueError(f"tiers.{tier} must be an object")
    known = {"agent", "memory_mode", "subagents", "skills", "pythia_ring_max", "strip_context", "tools", "mcp_servers", "disable_clarification", "silence_token", "prompt"}
    unknown = sorted(set(raw) - known)
    if unknown:
        logger.warning("[tier-policy] tiers.%s: unknown keys ignored: %s", tier, ", ".join(unknown))
    agent = raw.get("agent")
    if not isinstance(agent, str) or not _AGENT_NAME_RE.fullmatch(agent):
        raise ValueError(f"tiers.{tier}.agent must be an agent name")
    memory_mode = raw.get("memory_mode", "off")
    if memory_mode not in MEMORY_MODES:
        raise ValueError(f"tiers.{tier}.memory_mode must be one of {sorted(MEMORY_MODES)}")
    # Subagents do not inherit a tool ceiling, so version 1 cannot enable them.
    if raw.get("subagents", False) is not False:
        raise ValueError(f"tiers.{tier}.subagents must be false in version 1")
    ring = raw.get("pythia_ring_max", "internal")
    if ring not in PYTHIA_RINGS:
        raise ValueError(f"tiers.{tier}.pythia_ring_max must be one of {list(PYTHIA_RINGS)}")
    if "tools" not in raw:
        raise ValueError(f"tiers.{tier}.tools is required")
    tools = _string_list(raw["tools"], "tools", tier)
    skills = _string_list(raw.get("skills", []), "skills", tier)
    strip = _string_list(raw.get("strip_context", []), "strip_context", tier)
    servers_raw = raw.get("mcp_servers", {})
    if not isinstance(servers_raw, dict):
        raise ValueError(f"tiers.{tier}.mcp_servers must be an object of server key to URL")
    servers: dict[str, str] = {}
    for key, url in servers_raw.items():
        normalized = normalize_mcp_url(url)
        if not isinstance(key, str) or not key or normalized is None:
            raise ValueError(f"tiers.{tier}.mcp_servers.{key} must be an http(s) URL")
        servers[key] = normalized
    disable_clarification = raw.get("disable_clarification", True)
    if not isinstance(disable_clarification, bool):
        raise ValueError(f"tiers.{tier}.disable_clarification must be a boolean")
    silence_token = raw.get("silence_token")
    prompt = raw.get("prompt")
    for name, value in (("silence_token", silence_token), ("prompt", prompt)):
        if value is not None and not isinstance(value, str):
            raise ValueError(f"tiers.{tier}.{name} must be a string or null")
    return TierPolicy(
        tier=tier,
        agent=agent,
        tools=frozenset(tools),
        mcp_servers=servers,
        memory_mode=memory_mode,
        skills=frozenset(skills),
        pythia_ring_max=ring,
        strip_context=tuple(dict.fromkeys([*ALWAYS_STRIPPED_CONTEXT_KEYS, *strip])),
        disable_clarification=disable_clarification,
        silence_token=silence_token or None,
        prompt=prompt.strip() if isinstance(prompt, str) and prompt.strip() else None,
        tier_only_tools=tier_only_tools,
    )


def _parse(data: bytes) -> TierPolicies:
    raw = json.loads(data)
    if not isinstance(raw, dict):
        raise ValueError("the policy must be a JSON object")
    version = raw.get("version")
    if type(version) is not int or version != TIER_POLICY_VERSION:
        raise ValueError(f"unsupported version {version!r} (this gateway reads version {TIER_POLICY_VERSION})")
    tier_only = frozenset(_string_list(raw.get("tier_only_tools", []), "tier_only_tools", "*"))
    tiers_raw = raw.get("tiers")
    if not isinstance(tiers_raw, dict):
        raise ValueError("tiers must be an object")
    tiers: dict[str, TierPolicy] = {}
    for tier, entry in tiers_raw.items():
        if tier not in POLICY_TIERS:
            logger.warning("[tier-policy] tiers.%s ignored: policy tiers are %s", tier, ", ".join(sorted(POLICY_TIERS)))
            continue
        try:
            tiers[tier] = _parse_tier(tier, entry, tier_only)
        except ValueError as exc:
            # One bad tier refuses that tier only.
            logger.error("[tier-policy] tier %s refused: %s", tier, exc)
    return TierPolicies(version=version, sha256=hashlib.sha256(data).hexdigest(), tiers=tiers, tier_only_tools=tier_only)


def tier_policy_file_present() -> bool:
    """True when the image ships a policy file (valid or not)."""
    try:
        return TIER_POLICY_PATH.is_file()
    except OSError:
        return False


def load_tier_policies() -> TierPolicies | None:
    """The image's tier policies, or None when the file is missing or invalid.

    Cached on the file's identity (path, mtime, size), so a consumer on a hot
    path (the MCP interceptor) pays one ``stat`` per call.
    """
    global _cache
    path = TIER_POLICY_PATH
    try:
        stat = os.stat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.error("[tier-policy] %s unreadable (%s): restricted tiers are refused", path, type(exc).__name__)
        return None
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    with _cache_lock:
        if _cache is not None and _cache[0] == key:
            return _cache[1]
    try:
        policies: TierPolicies | None = _parse(path.read_bytes())
    except (OSError, ValueError) as exc:
        logger.error("[tier-policy] %s invalid (%s): restricted tiers are refused", path, exc)
        policies = None
    with _cache_lock:
        _cache = (key, policies)
    return policies


def reset_tier_policy_cache() -> None:
    global _cache
    with _cache_lock:
        _cache = None


def is_restricted_tier(tier: object) -> bool:
    """True for any named tier other than ``owner``/``dm`` (unknown values included)."""
    return isinstance(tier, str) and bool(tier) and tier not in UNRESTRICTED_TIERS


def policy_for(tier: object) -> TierPolicy | None:
    """The policy for a restricted tier, or None (refuse the tier)."""
    if not is_restricted_tier(tier):
        return None
    policies = load_tier_policies()
    if policies is None:
        return None
    return policies.tiers.get(tier)  # type: ignore[arg-type]


def stricter_tier(*tiers: object) -> str | None:
    """The strictest tier named by any carrier, or None when none names one."""
    named = [tier for tier in tiers if isinstance(tier, str) and tier]
    if not named:
        return None
    return max(named, key=lambda tier: _TIER_RANK.get(tier, _UNKNOWN_TIER_RANK))


def header_tier_value(tier: object) -> str:
    """The ``X-Argus-Run-Tier`` value for a run's tier."""
    if isinstance(tier, str) and _TIER_VALUE_RE.fullmatch(tier):
        return tier
    return OWNER_TIER if not tier else "public"


def resolve_run_tier_policy(cfg: Mapping[str, Any]) -> TierPolicy | None:
    """The policy a run's merged config asks for; raises when it names a tier none covers."""
    tier = cfg.get(ARGUS_TIER_KEY)
    if not is_restricted_tier(tier):
        return None
    policy = policy_for(tier)
    if policy is None:
        raise TierPolicyUnavailable(f"run tier {tier!r} has no valid policy in {TIER_POLICY_PATH.name}; refusing to build the agent")
    return policy


def apply_tier_policy(config: MutableMapping[str, Any], policy: TierPolicy, *, context: MutableMapping[str, Any] | None = None) -> None:
    """Pin a tiered run's ceiling in every run-config carrier. Overrides, never ``setdefault``.

    ``config`` is a RunnableConfig-shaped dict (its existing ``configurable``
    and ``context`` sections are copied before writing, so a shared session
    dict is never mutated); ``context`` is the request's top-level run context
    when the caller has one (the channel manager). Call it after every other
    write: it is what makes the policy win over ``session.context`` from
    ``config.yaml`` and over anything a client sent.
    """
    carriers: list[MutableMapping[str, Any]] = []
    runtime_carriers: list[MutableMapping[str, Any]] = []
    for section in ("configurable", "context"):
        value = config.get(section)
        if isinstance(value, Mapping):
            copied = dict(value)
            config[section] = copied
            carriers.append(copied)
            if section == "context":
                runtime_carriers.append(copied)
            else:
                # Runtime-only keys never ride the checkpointed section.
                copied.pop(ARGUS_TIER_KEY, None)
    if context is not None:
        carriers.append(context)
        runtime_carriers.append(context)
    for carrier in carriers:
        for key in policy.strip_context:
            carrier.pop(key, None)
        carrier["agent_name"] = policy.agent
        carrier["memory_mode"] = policy.memory_mode
        carrier["subagent_enabled"] = False
        carrier["is_bootstrap"] = False
    for carrier in runtime_carriers:
        carrier[ARGUS_TIER_KEY] = policy.tier
        if policy.disable_clarification:
            carrier["disable_clarification"] = True


async def admit_run_tier(config: MutableMapping[str, Any], thread_store: Any, thread_id: str, run_metadata: MutableMapping[str, Any] | None = None) -> TierPolicy | None:
    """Run admission: bind the run to its thread's tier and apply the policy.

    The tier is the strictest of the run's context, its configurable section
    and the thread's server-owned metadata, so a run started anywhere (web UI,
    Chronos, atlas-mcp) on a public thread is a public run. A tiered run stamps
    its thread (``run_metadata`` carries the stamp into a thread row the run
    itself creates). Raises :class:`TierPolicyUnavailable` when the tier has no
    valid policy.

    The thread row is read (unscoped, so a NULL-owner row counts too) only when
    the image ships a policy file: without one no tier can run, and an image
    without the file is a broken build, equivalent to a pre-#174 rollback.
    A store error propagates (the run is refused), it never reads as "no tier".
    """
    if run_metadata is not None:
        run_metadata.pop(ARGUS_TIER_KEY, None)
    context = config.get("context")
    configurable = config.get("configurable")
    context_tier = context.get(ARGUS_TIER_KEY) if isinstance(context, Mapping) else None
    configurable_tier = configurable.get(ARGUS_TIER_KEY) if isinstance(configurable, Mapping) else None
    thread_tier = None
    record = await thread_store.get(thread_id, user_id=None) if thread_store is not None and tier_policy_file_present() else None
    metadata = record.get("metadata") if isinstance(record, Mapping) else None
    if isinstance(metadata, Mapping):
        thread_tier = metadata.get(ARGUS_TIER_KEY)
    tier = stricter_tier(context_tier, configurable_tier, thread_tier)
    if not is_restricted_tier(tier):
        return None
    policy = policy_for(tier)
    if policy is None:
        raise TierPolicyUnavailable(f"run tier {tier!r} has no valid policy")
    config.setdefault("context", {})
    apply_tier_policy(config, policy)
    if thread_tier != policy.tier:
        if record is not None:
            await thread_store.update_metadata(thread_id, {ARGUS_TIER_KEY: policy.tier}, touch=False, user_id=None)
        if run_metadata is not None:
            run_metadata[ARGUS_TIER_KEY] = policy.tier
    return policy


def tier_agent_present(agent_name: str, *, user_id: str | None = None) -> bool:
    """Whether the stack has the policy's agent (a missing agent refuses the tier)."""
    try:
        from deerflow.config.agents_config import load_agent_config

        return load_agent_config(agent_name, user_id=user_id) is not None
    except Exception:
        return False


def clamp_pythia_ring(ring: str, policy: TierPolicy | None) -> str:
    """The Pythia retrieval ring, capped at the policy's ``pythia_ring_max``."""
    if policy is None:
        return ring
    order = {name: index for index, name in enumerate(PYTHIA_RINGS)}
    current = order.get((ring or "none").strip().lower(), 0)
    return PYTHIA_RINGS[min(current, order[policy.pythia_ring_max])]


def _global_mcp_server_urls() -> dict[str, str | None]:
    """Server key -> normalized URL from the deployment's extensions config (never the personal one)."""
    from deerflow.config.extensions_config import get_extensions_config

    servers = get_extensions_config().get_enabled_mcp_servers()
    return {name: normalize_mcp_url(server.url) if server.type in ("http", "sse") else None for name, server in servers.items()}


def mcp_server_allowed(server_name: object, policy: TierPolicy, *, server_urls: Mapping[str, str | None] | None = None) -> bool:
    """A deployment MCP server whose URL is on the policy's list.

    The server is looked up by name in the global extensions config, so a
    personal MCP server (whose names never collide with deployment names) is
    never allowed, and a citizen alias (``"pythia": <another URL>``) fails the
    URL check.
    """
    if not isinstance(server_name, str) or not server_name:
        return False
    urls = _global_mcp_server_urls() if server_urls is None else server_urls
    url = urls.get(server_name)
    return url is not None and url in policy.mcp_urls


def tier_tool_allowed(tool: Any, policy: TierPolicy, *, server_urls: Mapping[str, str | None] | None = None) -> bool:
    """Whether one bound tool fits the tier's ceiling.

    In-process: the name is on the policy's list and does not carry the prefix
    of a policy MCP server (``pythia_query`` can only come from the allowlisted
    ``pythia`` URL). MCP: the name is on the list and its server passes
    :func:`mcp_server_allowed`.
    """
    from deerflow.tools.mcp_metadata import get_mcp_source, is_mcp_tool

    name = getattr(tool, "name", None)
    if not isinstance(name, str) or name not in policy.tools:
        return False
    if is_mcp_tool(tool):
        source = get_mcp_source(tool)
        return source is not None and mcp_server_allowed(source["server_name"], policy, server_urls=server_urls)
    return not any(name.startswith(f"{key}_") for key in policy.mcp_servers)


def filter_tools_for_tier[ToolT](tools: Iterable[ToolT], policy: TierPolicy | None) -> list[ToolT]:
    """Apply the tier ceiling to an assembled tool list.

    With a policy: only tools that pass :func:`tier_tool_allowed`. Without one
    (owner/dm runs): the policy file's ``tier_only_tools`` are dropped, so an
    owner's prompt never carries a channel-thread-only tool.
    """
    tools = list(tools)
    if policy is None:
        policies = load_tier_policies()
        if policies is None or not policies.tier_only_tools:
            return tools
        return [tool for tool in tools if getattr(tool, "name", None) not in policies.tier_only_tools]
    server_urls = _global_mcp_server_urls() if any(_is_mcp(tool) for tool in tools) else {}
    kept = [tool for tool in tools if tier_tool_allowed(tool, policy, server_urls=server_urls)]
    dropped = sorted({str(getattr(tool, "name", "?")) for tool in tools} - {str(getattr(tool, "name", "?")) for tool in kept})
    if dropped:
        logger.info("[tier-policy] tier %s: %d tool(s) outside the ceiling dropped: %s", policy.tier, len(dropped), ", ".join(dropped))
    return kept


def _is_mcp(tool: Any) -> bool:
    from deerflow.tools.mcp_metadata import is_mcp_tool

    return is_mcp_tool(tool)


class TierToolViewError(RuntimeError):
    """A tiered build would bind a tool outside the tier's ceiling."""


def verify_tier_tool_view(tools: Iterable[Any], middlewares: Iterable[Any], policy: TierPolicy) -> None:
    """Final assertion before binding: every bound and middleware-declared tool fits the ceiling."""
    bound = list(tools)
    declared = [tool for middleware in middlewares for tool in (getattr(middleware, "tools", None) or ())]
    candidates = [*bound, *declared]
    server_urls = _global_mcp_server_urls() if any(_is_mcp(tool) for tool in candidates) else {}
    outside = sorted({str(getattr(tool, "name", tool)) for tool in candidates if not tier_tool_allowed(tool, policy, server_urls=server_urls)})
    if outside:
        raise TierToolViewError(f"tier {policy.tier}: refusing to bind tools outside the ceiling: {', '.join(outside)}")


def guard_tier_middlewares(middlewares: Iterable[Any], tools: Iterable[Any], policy: TierPolicy) -> list[Any]:
    """Narrow middleware-declared tools to the ceiling and put the call-time guard outermost.

    LangChain binds ``middleware.tools`` after the host's tool list, so a
    declaring middleware would otherwise bypass the assembly filter; the
    narrowing reuses the build-local view of ``tool_declarations``. The guard's
    allowed set is exactly what this build binds.
    """
    from deerflow.agents.middlewares.tier_tool_guard_middleware import TierToolGuardMiddleware
    from deerflow.agents.middlewares.tool_declarations import apply_declared_tool_view

    stack = list(middlewares)
    bound = list(tools)
    declared = [tool for middleware in stack for tool in (getattr(middleware, "tools", None) or ())]
    server_urls = _global_mcp_server_urls() if any(_is_mcp(tool) for tool in [*bound, *declared]) else {}
    declared_allowed = {tool.name for tool in declared if tier_tool_allowed(tool, policy, server_urls=server_urls)}
    stack = apply_declared_tool_view(stack, authorized_names=declared_allowed)
    allowed = {tool.name for tool in bound if tier_tool_allowed(tool, policy, server_urls=server_urls)} | declared_allowed
    return [TierToolGuardMiddleware(policy.tier, allowed), *stack]
