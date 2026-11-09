"""[argus patch #174, #175] Run tiers: the ceiling a restricted run cannot widen.

A run tier says who the answer is for. A run without a tier, and ``dm``, are
the stack owner talking to their own Atlas and keep today's behaviour. The
restricted tiers take their rules from a policy file the platform bakes into
the image, never from anything a citizen can edit:

- ``public`` (#174): a Slack channel thread any colleague in the channel can
  read; a fixed tool ceiling, no memory, no personal data.
- ``owner`` (#175): a channel run the router verified its owner triggered. The
  owner's own agent and tools (minus subagents and ``update_agent``), memory
  read-only, the Pythia ring capped, and taint gating: the first PRIVATE read
  in a turn refuses that turn's writes and egress, and flags its answer posts
  (``X-Argus-Turn-Private``) so the router drafts them to the owner only.
- ``private`` (phase 3b): refused until a policy defines it.

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
          "prompt": "You are {persona}, answering in {channel_label} ({audience}) for <@{trigger_user}>."
        },
        "owner": {
          "memory_mode": "off",
          "pythia_ring_max": "internal",
          "mcp_servers": {"pythia": "http://argus-kb-api:8000/pythia/mcp"},
          "public_tools": ["pythia_query", "slack_channel_history", "web_search", "tool_search", "describe_skill"],
          "private_read_tools": ["calendar_list_events", "gmail_search_threads"],
          "self_write_tools": ["gmail_create_draft"],
          "write_tools": ["asana_create_task", "github_create_issue"],
          "egress_tools": ["web_search", "web_search_exa", "image_search", "web_fetch"],
          "prompt": "You are {persona}, answering {trigger_user} in {channel_label}."
        }
      }
    }

``tiers.owner`` has no ``agent`` or ``tools``: the agent is the stack's
``tier_agents.owner`` (else its default agent) and the ceiling is that agent's
own tool set minus :data:`OWNER_TIER_EXCLUDED_TOOLS`. ``public_tools``,
``private_read_tools``, ``self_write_tools``, ``write_tools`` and
``egress_tools`` are required lists (empty allowed); ``memory_mode`` defaults to
``read-only`` (``off``: nothing injected, nothing written), ``pythia_ring_max``
to ``internal``, ``subagents`` must be false. Classification
(:func:`classify_tool`) decides what taints a run:

- WRITE (never taints): named in ``write_tools`` or ``self_write_tools``;
- PUBLIC (never taints): named in ``public_tools`` and, when it is an MCP tool,
  from a deployment server whose URL is in ``mcp_servers`` (an in-process tool
  carrying a ``<server key>_`` prefix of those servers is not public);
- PRIVATE (taints): everything else, ``private_read_tools``, MCP tools and
  unknown names included. Memory injected into the turn (``memory_mode:
  read-only``) is a PRIVATE read too.

Before the first PRIVATE call a run has the owner's full ceiling. After it the
run is tainted and only an allowlist may run (:meth:`TaintClasses.permitted_after_taint`):
PUBLIC tools, ``private_read_tools`` and ``self_write_tools``, never anything
in ``egress_tools`` or ``write_tools`` even if listed elsewhere. Every other
call (writes, egress, ``bash``, browsers, sends, unknown tools) gets
:data:`TAINT_REFUSAL`. The tainted run's answer messages carry
:data:`TURN_PRIVATE_KEY` with short source labels (the MCP server key, else the
tool name's first word; ``memory``). ``mcp_servers`` also names the servers
whose caller token is withheld, which keeps Pythia at the internal ring; every
other server keeps the owner's identity.

Top-level keys other than ``version``, ``tier_only_tools`` and ``tiers`` (a
``_comment`` documentation array, say) are ignored. ``prompt`` is rendered with
``str.format_map`` over :data:`TIER_PROMPT_VARIABLES` from the relay's router
block (:func:`render_tier_prompt`); a variable the run does not carry renders
empty. ``silence_token`` is parsed and not used by the gateway: the router drops
a channel post that is exactly the token.

Fail closed: a missing, unreadable or invalid file, an unknown ``version``, or
a missing or invalid tier entry means :func:`policy_for` returns ``None`` and
every consumer refuses that tier (the relay refuses the event, run admission
refuses the run, assembly fails the build, the MCP interceptor refuses the
call). Nothing falls back to the owner's full agent.

Enforcement points (each one alone holds the ceiling):

- ``app/channels/slack.py`` ``relay_event``: the public tier runs the policy's
  agent, never ``tier_agents`` and never the channel default; the owner tier
  runs ``tier_agents.owner`` or the default agent, and the adapter sends a
  tainted answer with the ``X-Argus-Turn-*`` headers.
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

import asyncio
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
# runtime.context key carrying the relay's prompt variables (internal-only).
ARGUS_TIER_VARS_KEY = "argus_tier_vars"
# The router-block fields policy.prompt may name as {placeholders}.
TIER_PROMPT_VARIABLES = ("persona", "channel_label", "audience", "trigger_user")
_TIER_PROMPT_VALUE_MAX_CHARS = 200
# The per-call MCP header (mcp/run_tier.py).
RUN_TIER_HEADER = "X-Argus-Run-Tier"
# [argus patch #175] The owner tier: a channel run its owner triggered. Also
# the header value for a run that names no tier (both are the full owner to
# atlas-mcp and kb-api; the gateway enforces the owner tier's caps itself).
OWNER_TIER = "owner"
# The owner talking to their own Atlas in their DM: no policy, no restriction.
UNRESTRICTED_TIERS = frozenset({"dm"})
# Tiers a policy may define.
POLICY_TIERS = frozenset({"public", "private", OWNER_TIER})
# Strictest wins when two carriers name different tiers. An unknown tier ranks
# above everything so it is the one chosen, and then refused (no policy).
_TIER_RANK = {"dm": 0, OWNER_TIER: 1, "private": 2, "public": 3}
_UNKNOWN_TIER_RANK = 4
# Never bound in an owner-tier run: subagents and external agents run outside
# the taint guard, and update_agent/setup_agent would let channel text rewrite
# the owner's agent for every later run.
OWNER_TIER_EXCLUDED_TOOLS = frozenset({"task", "batch_task", "batch_status", "cancel_batch", "read_batch_result", "invoke_acp_agent", "update_agent", "setup_agent"})

# [argus patch #175] Taint: the AIMessage additional_kwargs key (and outbound
# metadata key) carrying a tainted run's source labels, and the relay headers.
TURN_PRIVATE_KEY = "argus_turn_private"
TURN_PRIVATE_HEADER = "X-Argus-Turn-Private"
TURN_SOURCES_HEADER = "X-Argus-Turn-Sources"
TAINT_REFUSAL = "This turn read private data, so it can't write or search the web. Ask again in a new message to act."
MEMORY_SOURCE_LABEL = "memory"
_LABEL_RE = re.compile(r"[^a-z0-9_-]+")
_MAX_LABELS = 16
_MAX_LABEL_CHARS = 32

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
class TaintRules:
    """[argus patch #175] The owner tier's tool classes (names)."""

    public_tools: frozenset[str]
    write_tools: frozenset[str]
    egress_tools: frozenset[str]
    # Allowed after taint: reads (they taint) and writes that stay in the owner's accounts.
    private_read_tools: frozenset[str] = frozenset()
    self_write_tools: frozenset[str] = frozenset()


@dataclass(frozen=True)
class TierPolicy:
    tier: str
    # None: the run keeps its own agent (the owner tier).
    agent: str | None
    # None: no fixed ceiling, the agent's own tools minus OWNER_TIER_EXCLUDED_TOOLS.
    tools: frozenset[str] | None
    mcp_servers: Mapping[str, str]  # server key -> normalized URL
    memory_mode: str = "off"
    # None: the agent's own skills.
    skills: frozenset[str] | None = frozenset()
    pythia_ring_max: str = "internal"
    strip_context: tuple[str, ...] = ()
    disable_clarification: bool = True
    silence_token: str | None = None
    prompt: str | None = None
    subagents: bool = False
    tier_only_tools: frozenset[str] = frozenset()
    taint: TaintRules | None = None

    @property
    def mcp_urls(self) -> frozenset[str]:
        return frozenset(self.mcp_servers.values())

    @property
    def has_ceiling(self) -> bool:
        return self.tools is not None

    @property
    def thread_bound(self) -> bool:
        """Whether admission stamps the tier on the thread (not the owner tier)."""
        return self.taint is None

    @property
    def reads_memory(self) -> bool:
        """Memory is injected (never written): only a taint-tracked tier may read it."""
        return self.taint is not None and self.memory_mode == "read-only"


@dataclass(frozen=True)
class TierPolicies:
    version: int
    sha256: str
    tiers: Mapping[str, TierPolicy] = field(default_factory=dict)
    tier_only_tools: frozenset[str] = frozenset()


_cache_lock = threading.Lock()
_cache: tuple[Any, TierPolicies | None] | None = None
# The last observed presence of the policy file (None: never checked). Every
# filesystem check below records it, so callers that may sit on an event loop
# read it without touching the disk (see last_known_tier_policy_file_present).
_file_present: bool | None = None


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


def _parse_owner_tier(raw: dict, tier_only_tools: frozenset[str]) -> TierPolicy:
    """[argus patch #175] ``tiers.owner``: classes instead of a ceiling."""
    tier = OWNER_TIER
    known = {"memory_mode", "subagents", "pythia_ring_max", "strip_context", "mcp_servers", "public_tools", "private_read_tools", "self_write_tools", "write_tools", "egress_tools", "disable_clarification", "silence_token", "prompt"}
    unknown = sorted(key for key in set(raw) - known if not str(key).startswith("_"))
    if unknown:
        logger.warning("[tier-policy] tiers.owner: keys ignored (the agent is tier_agents.owner, the ceiling its own tools): %s", ", ".join(unknown))
    memory_mode = raw.get("memory_mode", "read-only")
    if memory_mode not in MEMORY_MODES:
        raise ValueError(f"tiers.owner.memory_mode must be one of {sorted(MEMORY_MODES)}")
    if raw.get("subagents", False) is not False:
        raise ValueError("tiers.owner.subagents must be false in version 1")
    ring = raw.get("pythia_ring_max", "internal")
    if ring not in PYTHIA_RINGS:
        raise ValueError(f"tiers.owner.pythia_ring_max must be one of {list(PYTHIA_RINGS)}")
    classes = {}
    for name in ("public_tools", "private_read_tools", "self_write_tools", "write_tools", "egress_tools"):
        if name not in raw:
            raise ValueError(f"tiers.owner.{name} is required")
        classes[name] = frozenset(_string_list(raw[name], name, tier))
    common = _parse_common(tier, raw)
    return TierPolicy(
        tier=tier,
        agent=None,
        tools=None,
        memory_mode=memory_mode,
        skills=None,
        pythia_ring_max=ring,
        tier_only_tools=tier_only_tools,
        taint=TaintRules(**classes),
        **common,
    )


def _parse_common(tier: str, raw: dict) -> dict[str, Any]:
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
    return {
        "mcp_servers": servers,
        "strip_context": tuple(dict.fromkeys([*ALWAYS_STRIPPED_CONTEXT_KEYS, *strip])),
        "disable_clarification": disable_clarification,
        "silence_token": silence_token or None,
        "prompt": prompt.strip() if isinstance(prompt, str) and prompt.strip() else None,
    }


def _parse_tier(tier: str, raw: object, tier_only_tools: frozenset[str]) -> TierPolicy:
    if not isinstance(raw, dict):
        raise ValueError(f"tiers.{tier} must be an object")
    if tier == OWNER_TIER:
        return _parse_owner_tier(raw, tier_only_tools)
    known = {"agent", "memory_mode", "subagents", "skills", "pythia_ring_max", "strip_context", "tools", "mcp_servers", "disable_clarification", "silence_token", "prompt"}
    unknown = sorted(key for key in set(raw) - known if not str(key).startswith("_"))
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
    return TierPolicy(
        tier=tier,
        agent=agent,
        tools=frozenset(tools),
        memory_mode=memory_mode,
        skills=frozenset(skills),
        pythia_ring_max=ring,
        tier_only_tools=tier_only_tools,
        **_parse_common(tier, raw),
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
    """True when the image ships a policy file (valid or not). Blocking: a ``stat``."""
    global _file_present
    try:
        present = TIER_POLICY_PATH.is_file()
    except OSError:
        present = False
    _file_present = present
    return present


def last_known_tier_policy_file_present() -> bool:
    """The last recorded presence of the policy file; one ``stat`` only if nothing checked yet.

    For synchronous callers that may run on an event loop (the MCP
    interceptor builder at discovery time). Every off-loop policy load
    refreshes the value, and the file is image-baked, so it does not change
    under a running gateway.
    """
    present = _file_present
    return tier_policy_file_present() if present is None else present


def load_tier_policies() -> TierPolicies | None:
    """The image's tier policies, or None when the file is missing or invalid.

    Blocking (a ``stat``, and a read when the file changed): async callers go
    through :func:`aload_tier_policies` or ``asyncio.to_thread``. Cached on the
    file's identity (path, mtime, size), so every call sees an edited, replaced
    or removed file and pays one ``stat`` otherwise.
    """
    global _cache, _file_present
    path = TIER_POLICY_PATH
    try:
        stat = os.stat(path)
    except FileNotFoundError:
        _file_present = False
        with _cache_lock:
            _cache = (None, None)
        return None
    except OSError as exc:
        logger.error("[tier-policy] %s unreadable (%s): restricted tiers are refused", path, type(exc).__name__)
        with _cache_lock:
            _cache = (None, None)
        return None
    _file_present = True
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


async def aload_tier_policies() -> TierPolicies | None:
    """:func:`load_tier_policies` off the event loop."""
    return await asyncio.to_thread(load_tier_policies)


def cached_policy_for(tier: object) -> TierPolicy | None:
    """The policy for a restricted tier from the last load, without touching the disk.

    For synchronous code on the event loop (the channel manager). The relay
    loads the policy off the loop moments before a relayed message reaches the
    manager, so the cache is warm; run admission re-checks the file anyway.
    """
    if not is_restricted_tier(tier):
        return None
    with _cache_lock:
        cached = _cache
    policies = cached[1] if cached is not None else None
    return policies.tiers.get(tier) if policies is not None else None  # type: ignore[arg-type]


def reset_tier_policy_cache() -> None:
    global _cache, _file_present
    with _cache_lock:
        _cache = None
    _file_present = None


def is_restricted_tier(tier: object) -> bool:
    """True for any named tier other than ``dm`` (``owner`` and unknown values included)."""
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
    """The ``X-Argus-Run-Tier`` value for a run's tier (``owner`` for no tier)."""
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
        if policy.agent is not None:
            # The owner tier (#175) keeps the run's own agent.
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
    The policy file is checked and read off the event loop.
    """
    if run_metadata is not None:
        run_metadata.pop(ARGUS_TIER_KEY, None)
    context = config.get("context")
    configurable = config.get("configurable")
    context_tier = context.get(ARGUS_TIER_KEY) if isinstance(context, Mapping) else None
    configurable_tier = configurable.get(ARGUS_TIER_KEY) if isinstance(configurable, Mapping) else None
    thread_tier = None
    present, policies = await asyncio.to_thread(_policy_file_state)
    record = await thread_store.get(thread_id, user_id=None) if thread_store is not None and present else None
    metadata = record.get("metadata") if isinstance(record, Mapping) else None
    if isinstance(metadata, Mapping):
        thread_tier = metadata.get(ARGUS_TIER_KEY)
    tier = stricter_tier(context_tier, configurable_tier, thread_tier)
    if not is_restricted_tier(tier):
        return None
    policy = policies.tiers.get(tier) if policies is not None else None  # type: ignore[arg-type]
    if policy is None:
        raise TierPolicyUnavailable(f"run tier {tier!r} has no valid policy")
    config.setdefault("context", {})
    apply_tier_policy(config, policy)
    # [argus patch #175] The owner tier is not bound to the thread: a later run
    # there (the owner on the web) is not a channel run.
    if thread_tier != policy.tier and policy.thread_bound:
        if record is not None:
            await thread_store.update_metadata(thread_id, {ARGUS_TIER_KEY: policy.tier}, touch=False, user_id=None)
        if run_metadata is not None:
            run_metadata[ARGUS_TIER_KEY] = policy.tier
    return policy


def tier_prompt_variables(metadata: Mapping[str, Any] | None) -> dict[str, str]:
    """The prompt variables a relayed message carries (router-block strings, whitespace collapsed, capped)."""
    variables: dict[str, str] = {}
    for name in TIER_PROMPT_VARIABLES:
        value = (metadata or {}).get(name)
        if isinstance(value, str):
            cleaned = " ".join(value.split())[:_TIER_PROMPT_VALUE_MAX_CHARS]
            if cleaned:
                variables[name] = cleaned
    return variables


class _BlankForMissing(dict):
    def __missing__(self, key: str) -> str:
        return ""


def render_tier_prompt(policy: TierPolicy, variables: object = None) -> str | None:
    """``policy.prompt`` with its ``{placeholders}`` filled; the raw prompt if it cannot be formatted.

    Only :data:`TIER_PROMPT_VARIABLES` are offered; any other name, or a
    variable the run does not carry (a later web run on the thread), renders
    as an empty string. A prompt ``str.format_map`` cannot handle (a stray
    brace, a positional field, an attribute lookup) is appended as written,
    with a warning: the platform text is never dropped and the build never
    fails over it.
    """
    if not policy.prompt:
        return None
    values = _BlankForMissing()
    if isinstance(variables, Mapping):
        values.update({name: value for name, value in tier_prompt_variables(variables).items()})
    try:
        return policy.prompt.format_map(values)
    except Exception as exc:
        logger.warning("[tier-policy] tier %s prompt could not be rendered (%s); appending it as written", policy.tier, type(exc).__name__)
        return policy.prompt


def _policy_file_state() -> tuple[bool, TierPolicies | None]:
    """Blocking: whether the policy file exists, and its policies."""
    if not tier_policy_file_present():
        return False, None
    return True, load_tier_policies()


def restricted_mcp_call_decision(tier: str, server_name: object) -> tuple[str | None, bool]:
    """Blocking: ``(why the call is refused or None, whether to withhold the caller token)``.

    A tier with a ceiling (public) may call only its listed servers and never
    sends a caller token. The owner tier (#175) may call every server and
    withholds the token only on its listed servers (Pythia: the internal ring).
    """
    policy = policy_for(tier)
    if policy is None:
        return f"run tier {tier!r} has no valid policy", True
    listed = mcp_server_allowed(server_name, policy)
    if policy.has_ceiling:
        return (None if listed else f"not on the {tier} tier's server list"), True
    return None, listed


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
    if not isinstance(name, str):
        return False
    if policy.tools is None:
        # [argus patch #175] The owner tier: the agent's own tools minus the exclusions.
        return name not in OWNER_TIER_EXCLUDED_TOOLS
    if name not in policy.tools:
        return False
    if is_mcp_tool(tool):
        source = get_mcp_source(tool)
        return source is not None and mcp_server_allowed(source["server_name"], policy, server_urls=server_urls)
    return not any(name.startswith(f"{key}_") for key in policy.mcp_servers)


def filter_tools_for_tier[ToolT](tools: Iterable[ToolT], policy: TierPolicy | None) -> list[ToolT]:
    """Apply the tier ceiling to an assembled tool list.

    With a policy: only tools that pass :func:`tier_tool_allowed`. Without one
    (untiered/dm runs): the policy file's ``tier_only_tools`` are dropped, so an
    owner's DM or web prompt never carries a channel-thread-only tool.
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
    taint = None
    if policy.taint is not None:
        # [argus patch #175] Classes for every name this build can call; any
        # other name the model produces is PRIVATE.
        rules = policy.taint
        taint = TaintClasses(
            classes={tool.name: classify_tool(tool, policy, server_urls=server_urls) for tool in [*bound, *declared] if isinstance(getattr(tool, "name", None), str)},
            egress=rules.egress_tools,
            never_after_taint=rules.egress_tools | rules.write_tools,
            allowed_after_taint=(rules.private_read_tools | rules.self_write_tools) - rules.egress_tools - rules.write_tools,
        )
    return [TierToolGuardMiddleware(policy.tier, allowed, taint=taint), *stack]


# -- [argus patch #175] taint ---------------------------------------------------


def source_label(name: object) -> str:
    """A short, header-safe label for a private source (an MCP server key or a tool name)."""
    text = str(name or "").strip().lower()
    head = text.split("_", 1)[0] if "_" in text else text
    return _LABEL_RE.sub("-", head).strip("-")[:_MAX_LABEL_CHARS] or "tool"


@dataclass(frozen=True)
class ToolClass:
    kind: str  # "public" | "write" | "private"
    label: str


@dataclass(frozen=True)
class TaintClasses:
    """A build's tool classes for the taint guard; unknown names are PRIVATE."""

    classes: Mapping[str, ToolClass]
    egress: frozenset[str]
    # egress_tools | write_tools: refused after taint whatever else lists them.
    never_after_taint: frozenset[str] = frozenset()
    # private_read_tools | self_write_tools, minus never_after_taint.
    allowed_after_taint: frozenset[str] = frozenset()

    def of(self, name: object) -> ToolClass:
        found = self.classes.get(name) if isinstance(name, str) else None
        return found if found is not None else ToolClass("private", source_label(name))

    def permitted_after_taint(self, name: object) -> bool:
        """The allowlist a tainted run is held to: PUBLIC tools, private reads, self-writes."""
        if not isinstance(name, str) or name in self.never_after_taint or name in self.egress:
            return False
        return self.of(name).kind == "public" or name in self.allowed_after_taint


def classify_tool(tool: Any, policy: TierPolicy, *, server_urls: Mapping[str, str | None] | None = None) -> ToolClass:
    """WRITE, PUBLIC or PRIVATE for one tool under the owner tier's classes (default PRIVATE)."""
    from deerflow.tools.mcp_metadata import get_mcp_source, is_mcp_tool

    name = getattr(tool, "name", None)
    rules = policy.taint
    source = get_mcp_source(tool) if is_mcp_tool(tool) else None
    label = source_label(source["server_name"] if source else name)
    if rules is None or not isinstance(name, str):
        return ToolClass("private", label)
    if name in rules.write_tools or name in rules.self_write_tools:
        return ToolClass("write", label)
    if name in rules.public_tools:
        if is_mcp_tool(tool):
            if source is not None and mcp_server_allowed(source["server_name"], policy, server_urls=server_urls):
                return ToolClass("public", label)
        elif not any(name.startswith(f"{key}_") for key in policy.mcp_servers):
            return ToolClass("public", label)
    return ToolClass("private", label)


def _message_field(message: Any, key: str) -> Any:
    return message.get(key) if isinstance(message, Mapping) else getattr(message, key, None)


# DynamicContextMiddleware's marker on its injected messages (its
# _DYNAMIC_CONTEXT_REMINDER_KEY; spelled out so this module stays import-light,
# a test pins the two together). Memory rides a HumanMessage ``<id>__memory``.
_DYNAMIC_CONTEXT_REMINDER_KEY = "dynamic_context_reminder"


def _is_injected_memory(message: Any) -> bool:
    kwargs = _message_field(message, "additional_kwargs") or {}
    message_type = _message_field(message, "type")
    return message_type == "human" and str(_message_field(message, "id") or "").endswith("__memory") and kwargs.get(_DYNAMIC_CONTEXT_REMINDER_KEY) is True


def current_run_messages(messages: Iterable[Any], pre_existing_ids: object) -> list[Any]:
    """The messages this run added: not in the worker's pre-run id set.

    Without that set (an embedded harness), everything after the last visible
    human message, plus the hidden messages injected just before it (memory).
    """
    messages = list(messages)
    if isinstance(pre_existing_ids, (set, frozenset, list, tuple)):
        known = {str(item) for item in pre_existing_ids if item}
        return [message for message in messages if str(_message_field(message, "id") or "") not in known]
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        kwargs = _message_field(message, "additional_kwargs") or {}
        if _message_field(message, "type") == "human" and not kwargs.get("hide_from_ui"):
            start = index
            while start > 0 and (_message_field(messages[start - 1], "additional_kwargs") or {}).get("hide_from_ui"):
                start -= 1
            return messages[start:]
    return messages


def run_private_sources(messages: Iterable[Any], taint: TaintClasses, *, exclude_call_id: object = None) -> list[str]:
    """Sorted labels of the PRIVATE reads in ``messages`` (one run's messages).

    ``exclude_call_id`` leaves one tool call out: the guard asks whether a call
    comes after a PRIVATE call, so the first PRIVATE call itself runs under the
    full ceiling, while a sibling in the same model step counts.
    """
    labels: set[str] = set()
    for message in messages:
        if _is_injected_memory(message):
            labels.add(MEMORY_SOURCE_LABEL)
        if _message_field(message, "type") != "ai":
            continue
        for call in _message_field(message, "tool_calls") or ():
            name = call.get("name") if isinstance(call, Mapping) else getattr(call, "name", None)
            call_id = call.get("id") if isinstance(call, Mapping) else getattr(call, "id", None)
            if exclude_call_id is not None and call_id == exclude_call_id:
                continue
            cls = taint.of(name)
            if cls.kind == "private":
                labels.add(cls.label)
    return sorted(labels)[:_MAX_LABELS]


def turn_private_labels(messages: Iterable[Any]) -> list[str]:
    """The labels a run's answer messages were stamped with (the channel manager's read)."""
    labels: set[str] = set()
    for message in messages:
        if _message_field(message, "type") != "ai":
            continue
        stamp = (_message_field(message, "additional_kwargs") or {}).get(TURN_PRIVATE_KEY)
        if isinstance(stamp, (list, tuple)):
            labels.update(source_label(item) for item in stamp if isinstance(item, str) and item)
    return sorted(labels)[:_MAX_LABELS]


def turn_private_headers(labels: Iterable[str]) -> dict[str, str]:
    """The relay headers for a tainted turn's answer posts."""
    clean = sorted({source_label(label) for label in labels if label})[:_MAX_LABELS]
    return {TURN_PRIVATE_HEADER: "1", TURN_SOURCES_HEADER: ",".join(clean)}
