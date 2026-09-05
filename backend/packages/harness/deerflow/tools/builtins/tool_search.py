"""Tool search — deferred tool discovery at runtime.

Contains:
- DeferredToolCatalog: immutable, searchable catalog of deferred tools.
- build_tool_search_tool: builds the `tool_search` tool as a closure over a
  catalog; it records promotions into graph state via ``Command``.
- build_deferred_tool_setup: assembles the catalog + tool from the tools
  configured for this agent build.
- build_mcp_routing_middleware: builds the PR2 auto-promote middleware from
  serialized routing metadata on deferred tools available to the caller.

An optional operator provider can add app/connector descriptors to the search
response. These descriptors are model-facing metadata only; they never enter
the MCP catalog hash or execute during discovery.

The agent sees deferred tool names in <available-deferred-tools> but cannot
call them until it fetches their full schema via the tool_search tool. The
deferred set rides on a build-time closure and promotion lives in per-thread
graph state — there is no ContextVar. Source-agnostic: a tool is "deferred"
when it carries the ``deerflow_mcp`` metadata tag.
"""

import hashlib
import html
import json
import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING, Annotated, Any

from langchain.tools import BaseTool, ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langchain_core.utils.function_calling import convert_to_openai_function
from langgraph.types import Command

from deerflow.tools.builtins.custom_discovery import CustomDiscoveryProvider, invoke_custom_provider
from deerflow.tools.mcp_metadata import get_mcp_routing, is_mcp_tool

if TYPE_CHECKING:
    from langchain.agents.middleware import AgentMiddleware

logger = logging.getLogger(__name__)

MAX_RESULTS = 5  # Max tools returned per search
_CUSTOM_NAME_PREFIXES = ("app_function:", "connector_function:")


def _runtime_visible_direct_names(runtime: ToolRuntime | None, configured_names: frozenset[str]) -> frozenset[str]:
    """Resolve direct names visible to the current model policy.

    Assembly only knows configured tools. The policy middleware stores the
    decision used for the preceding model request in runtime context, which
    lets exact-selection reporting distinguish visible tools from configured
    tools hidden by an active skill without guessing at assembly time.
    """
    if runtime is None:
        return frozenset()
    context = getattr(runtime, "context", None)
    if not isinstance(context, dict):
        return frozenset()
    try:
        from deerflow.runtime.secret_context import SKILL_TOOL_POLICY_DECISION_CONTEXT_KEY

        decision = context.get(SKILL_TOOL_POLICY_DECISION_CONTEXT_KEY)
    except Exception:
        decision = None
    if not isinstance(decision, dict):
        return frozenset()
    allowed = decision.get("allowed_names")
    if allowed is None:
        return configured_names
    if isinstance(allowed, list) and all(isinstance(name, str) for name in allowed):
        return frozenset(configured_names.intersection(allowed))
    return frozenset()


def _custom_provider_permitted(runtime: ToolRuntime | None) -> bool:
    """Avoid a provider call when the persisted policy denies both invokers.

    A missing or malformed decision is treated conservatively as unknown: the
    policy middleware remains the final descriptor filter, while discovery can
    still report useful local matches.
    """
    if runtime is None:
        return True
    context = getattr(runtime, "context", None)
    if not isinstance(context, dict):
        return True
    try:
        from deerflow.runtime.secret_context import SKILL_TOOL_POLICY_DECISION_CONTEXT_KEY

        decision = context.get(SKILL_TOOL_POLICY_DECISION_CONTEXT_KEY)
    except Exception:
        return True
    if not isinstance(decision, dict):
        return True
    allowed = decision.get("allowed_names")
    if not isinstance(allowed, list) or not all(isinstance(name, str) for name in allowed):
        return True
    return bool({"app_function_call", "connector_call"}.intersection(allowed))


def resolve_custom_provider(path: str | None) -> CustomDiscoveryProvider | None:
    """Resolve an operator configured provider at agent assembly time."""
    if path is None:
        return None
    if not isinstance(path, str):
        raise TypeError("custom_provider must be a dotted module.path:function string or None")
    if not path.strip():
        return None
    path = path.strip()
    from deerflow.reflection import resolve_variable

    provider = resolve_variable(path)
    if not callable(provider):
        raise ValueError(f"{path} is not a callable tool discovery provider")
    return provider


def _compile_catalog_regex(pattern: str) -> re.Pattern[str]:
    """Compile ``pattern`` case-insensitively, falling back to a literal match.

    Search queries come from the model, so an invalid regex (e.g. an unbalanced
    paren) must degrade to a literal substring match rather than raise.
    """
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:
        return re.compile(re.escape(pattern), re.IGNORECASE)


# ── Catalog ──


# NOTE: frozen=True without slots=True keeps __dict__, which is what lets the
# @cached_property fields below cache (they write to instance.__dict__, bypassing
# the frozen __setattr__). Do NOT add slots=True or hash/names break at runtime.
@dataclass(frozen=True)
class DeferredToolCatalog:
    """Immutable catalog of deferred tools. Pure search, no mutation."""

    tools: tuple[BaseTool, ...]

    @cached_property
    def names(self) -> frozenset[str]:
        return frozenset(t.name for t in self.tools)

    @cached_property
    def hash(self) -> str:
        canon = [{"name": t.name, "schema": convert_to_openai_function(t)} for t in sorted(self.tools, key=lambda t: t.name)]
        blob = json.dumps(canon, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def search(self, query: str) -> list[BaseTool]:
        query = query.strip()
        if not query:
            return []

        if query.startswith("select:"):
            # No cap: ``select:`` names the tools explicitly, so returning a
            # subset silently drops schemas the model asked for by name. Mirrors
            # ``SkillCatalog.search`` (``skills/catalog.py``); the ranked modes
            # below stay capped at ``MAX_RESULTS``.
            wanted = {n.strip() for n in query[7:].split(",")}
            return [t for t in self.tools if t.name in wanted]

        if query.startswith("+"):
            parts = query[1:].split(None, 1)
            if not parts:
                return []  # bare "+" with no required token — nothing to require
            required = parts[0].lower()
            candidates = [t for t in self.tools if required in t.name.lower()]
            if len(parts) > 1:
                candidates.sort(key=lambda t: _catalog_regex_score(parts[1], t), reverse=True)
            return candidates[:MAX_RESULTS]

        regex = _compile_catalog_regex(query)
        scored: list[tuple[int, BaseTool]] = []
        for t in self.tools:
            searchable = f"{t.name} {t.description or ''}"
            if regex.search(searchable):
                scored.append((2 if regex.search(t.name) else 1, t))
        scored.sort(key=lambda x: x[0], reverse=True)
        matches = [t for _, t in scored][:MAX_RESULTS]
        if matches:
            return matches

        # Ordinary queries are documented as keyword search, while retaining
        # regex matching for compatibility.  Only fall back after regex found
        # nothing, so an intentionally precise regex keeps its old behavior.
        tokens = [token for token in re.findall(r"[\w]+", query.casefold()) if token]
        if not tokens:
            return []
        ranked: list[tuple[int, int, str, BaseTool]] = []
        for candidate in self.tools:
            searchable = f"{candidate.name} {candidate.description or ''}".casefold()
            matched = sum(token in searchable for token in tokens)
            if matched:
                name_hits = sum(token in candidate.name.casefold() for token in tokens)
                ranked.append((matched, name_hits, candidate.name, candidate))
        ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
        return [tool for _, _, _, tool in ranked[:MAX_RESULTS]]


def _catalog_regex_score(pattern: str, t: BaseTool) -> int:
    regex = _compile_catalog_regex(pattern)
    return len(regex.findall(f"{t.name} {t.description or ''}"))


# ── Setup / tool ──


@dataclass(frozen=True)
class DeferredToolSetup:
    """Result of assembling deferred-tool support for one agent build.

    The three fields move as a unit, so callers branch on ``tool_search_tool``:

    - **Empty** ``(None, frozenset(), None)``: deferral is disabled, or no MCP
      tool is present in the candidate list. Nothing is deferred — bind tools
      as-is.
    - **Populated**: ``tool_search_tool`` is appended to the agent's tools,
      ``deferred_names`` are withheld from the model until promoted, and
      ``catalog_hash`` scopes those promotions in graph state.

    When an operator custom provider is configured, ``tool_search_tool`` may be
    present with an empty deferred catalog so app/connector discovery remains
    available even when this agent has no deferred MCP tools.
    """

    tool_search_tool: BaseTool | None
    deferred_names: frozenset[str]
    catalog_hash: str | None


def build_tool_search_tool(
    catalog: DeferredToolCatalog,
    directly_bound_names: frozenset[str] = frozenset(),
    *,
    runtime_visible_names: frozenset[str] = frozenset(),
    custom_provider: CustomDiscoveryProvider | None = None,
) -> BaseTool:
    catalog_hash = catalog.hash

    @tool
    def tool_search(query: str, tool_call_id: Annotated[str, InjectedToolCallId], runtime: ToolRuntime = None) -> Command:
        """Fetches full schema definitions for deferred tools so they can be called.

        Deferred tools appear by name in <available-deferred-tools> in the system
        prompt. Until fetched, only the name is known. This tool matches a query
        against the deferred tools and returns the matched tools complete schemas;
        once returned, a tool becomes callable.

        Query forms:
          - "select:Read,Edit" -- fetch these exact tools by name
          - "notebook jupyter" -- keyword search, up to max_results best matches
          - "+slack send" -- require "slack" in the name, rank by remaining terms

        When an operator custom provider is configured, ordinary searches can
        also return namespaced ``app_function:...`` or ``connector_function:...``
        descriptors. Their ``input_schema`` describes arguments for the
        existing ``app_function_call`` or ``connector_call`` invocation tool;
        the descriptor itself is metadata and is never promoted or executed.
        """
        started = time.perf_counter()
        clean_query = query.strip()
        is_select = clean_query.startswith("select:")
        requested = {n.strip() for n in clean_query[7:].split(",") if n.strip()} if is_select else set()
        # Exact MCP selection is local and deterministic.  Only a namespaced
        # custom ID opts into the operator provider; generic searches may use
        # the provider to discover app/connector functions.
        custom_allowed = custom_provider is not None and _custom_provider_permitted(runtime) and (not is_select or any(name.startswith(_CUSTOM_NAME_PREFIXES) for name in requested))
        custom_matches: list[dict[str, Any]] = []
        custom_status: dict[str, Any] = {"providers": {}, "complete": True}
        if custom_allowed:
            custom_matches, custom_status = invoke_custom_provider(custom_provider, clean_query)
            if is_select:
                custom_matches = [item for item in custom_matches if item.get("name") in requested]

        matched = catalog.search(query)
        local_names = {tool.name for tool in matched}
        policy_visible_names = _runtime_visible_direct_names(runtime, directly_bound_names)
        effective_visible_names = runtime_visible_names if runtime_visible_names else policy_visible_names
        already_visible = requested.intersection(effective_visible_names)
        # ``directly_bound_names`` is retained for old builders, but configured
        # names are deliberately not reported as runtime-visible: skill and
        # authorization middleware may have hidden those schemas.
        configured_only = requested.intersection(directly_bound_names) - already_visible
        names = [t.name for t in matched]
        if matched or custom_matches:
            # Keep ordinary search responses capped across all providers. Exact
            # selection remains uncapped because every named schema is explicit.
            custom_for_response = custom_matches if is_select else custom_matches[: max(0, MAX_RESULTS - len(matched))]
            schemas = [convert_to_openai_function(t) for t in matched] + custom_for_response
            content = json.dumps(schemas, indent=2, ensure_ascii=False)
            provider_failed = custom_allowed and not custom_status.get("complete", True)
            if is_select:
                unknown = requested - local_names - {item.get("name") for item in custom_matches} - already_visible - configured_only
                if already_visible or configured_only or unknown or provider_failed:
                    status_entry = {
                        "kind": "discovery_status",
                        "status": "partial" if provider_failed else "selection",
                        "already_visible": sorted(already_visible),
                        "configured_not_visible": sorted(configured_only),
                        "unknown": sorted(unknown),
                    }
                    content = json.dumps([*schemas, status_entry], indent=2, ensure_ascii=False)
            elif provider_failed:
                content = json.dumps(
                    [
                        *schemas,
                        {
                            "kind": "discovery_status",
                            "status": "partial",
                            "local_results_complete": True,
                            "providers": dict(custom_status.get("providers", {})),
                        },
                    ],
                    indent=2,
                    ensure_ascii=False,
                )
            status = {
                "local_matches": len(matched),
                "custom_matches": len(custom_matches),
                "provider_status": custom_status,
                "already_visible": sorted(already_visible),
                "configured_not_visible": sorted(configured_only),
                "unknown": sorted(requested - local_names - {item.get("name") for item in custom_matches} - already_visible - configured_only),
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            }
            message_kwargs = {"tool_search_timing": status}
            if provider_failed:
                message_kwargs["tool_search_partial"] = True
            return Command(
                update={
                    "promoted": {"catalog_hash": catalog_hash, "names": names},
                    "messages": [ToolMessage(content=content, tool_call_id=tool_call_id, name="tool_search", additional_kwargs=message_kwargs)],
                }
            )

        if not matched and not custom_matches:
            # Build-time binding does not guarantee runtime policy kept the schema visible.
            if already_visible:
                active_list = ", ".join(sorted(already_visible))
                content = f"Tool(s) '{active_list}' are already visible in the current tool list; call them directly. tool_search does not need to promote them."
            elif configured_only:
                active_list = ", ".join(sorted(configured_only))
                content = (
                    f"Tool(s) '{active_list}' are configured as directly bound, not deferred, but are not visible under the current runtime policy. "
                    "Call a tool directly only if its schema is present in your current tool list. "
                    "If its schema is absent, the current runtime policy does not allow it. "
                    "Do NOT retry tool_search for it."
                )
            else:
                content = f"No tools found matching: {query}"
                if custom_allowed and not custom_status.get("complete", True):
                    content += " Custom capability discovery is temporarily unavailable; local results are complete."
            if is_select:
                unknown = requested - local_names - {item.get("name") for item in custom_matches} - already_visible - configured_only
                if already_visible or configured_only or unknown or (custom_allowed and not custom_status.get("complete", True)):
                    content = json.dumps(
                        [
                            {
                                "kind": "discovery_status",
                                "status": "partial" if custom_allowed and not custom_status.get("complete", True) else "selection",
                                "message": content,
                                "already_visible": sorted(already_visible),
                                "configured_not_visible": sorted(configured_only),
                                "unknown": sorted(unknown),
                                "providers": dict(custom_status.get("providers", {})),
                            }
                        ],
                        indent=2,
                        ensure_ascii=False,
                    )
            names = []
        status = {
            "local_matches": 0,
            "custom_matches": 0,
            "provider_status": custom_status,
            "already_visible": sorted(already_visible),
            "configured_not_visible": sorted(configured_only),
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        }
        return Command(
            update={
                "promoted": {"catalog_hash": catalog_hash, "names": names},
                "messages": [
                    ToolMessage(
                        content=content,
                        tool_call_id=tool_call_id,
                        name="tool_search",
                        additional_kwargs={
                            "tool_search_timing": status,
                            "tool_search_partial": custom_allowed and not custom_status.get("complete", True),
                        },
                    )
                ],
            }
        )

    return tool_search


def _matches_any(name: str, patterns) -> bool:
    import fnmatch

    return any(fnmatch.fnmatchcase(name, pat) for pat in patterns or ())


def _is_excluded(name: str, exclude) -> bool:
    """True when a tool name matches any tool_search.always_bind/exclude pattern."""
    return _matches_any(name, exclude)


def _is_deferrable(tool: BaseTool, *, defer=(), exclude=()) -> bool:
    """Single deferral-eligibility predicate shared by setup and the fail-closed guard.

    MCP tools defer by default; non-MCP (builtin/config) tools defer only when
    named by a ``tool_search.defer`` pattern. ``always_bind``/``exclude`` wins
    over both — a pinned name never leaves the per-call schema set.
    """
    if _is_excluded(tool.name, exclude):
        return False
    return is_mcp_tool(tool) or _matches_any(tool.name, defer)


def build_deferred_tool_setup(
    candidate_tools: list[BaseTool],
    *,
    enabled: bool,
    exclude=(),
    defer=(),
    custom_provider: CustomDiscoveryProvider | None = None,
    runtime_visible_names: frozenset[str] = frozenset(),
) -> DeferredToolSetup:
    """Build deferred-tool setup from one agent build's candidate tools.

    Lead agents pass their full configured tool list; ``SkillToolPolicyMiddleware``
    later filters model-visible schemas, execution, and ``tool_search`` results
    for the active skill while keeping the discovery tool itself available.
    Subagents may pass a statically policy-filtered list because their configured
    skills are loaded at startup. The downstream deferred-schema middleware still
    hides unpromoted MCP schemas in either case.

    Returns an empty setup (see :class:`DeferredToolSetup`) in two distinct
    cases: deferral is disabled, or it is enabled but no MCP tool survived
    the caller's build-time selection.
    """
    if not enabled:
        # Deferral disabled: defer nothing; the model binds every tool as before.
        return DeferredToolSetup(None, frozenset(), None)
    deferred = [t for t in candidate_tools if _is_deferrable(t, defer=defer, exclude=exclude)]
    if not deferred and custom_provider is None:
        # Enabled, but no deferrable tool or custom catalog: no discovery tool
        # is needed and the upstream binding shape is preserved.
        return DeferredToolSetup(None, frozenset(), None)
    catalog = DeferredToolCatalog(tuple(deferred))
    directly_bound_names = frozenset(t.name for t in candidate_tools if t not in deferred)
    return DeferredToolSetup(
        build_tool_search_tool(
            catalog,
            directly_bound_names,
            runtime_visible_names=runtime_visible_names,
            custom_provider=custom_provider,
        ),
        catalog.names,
        catalog.hash,
    )


def assemble_deferred_tools(
    candidate_tools: list[BaseTool],
    *,
    enabled: bool,
    exclude=(),
    defer=(),
    custom_provider: CustomDiscoveryProvider | None = None,
    runtime_visible_names: frozenset[str] = frozenset(),
) -> tuple[list[BaseTool], DeferredToolSetup]:
    """Build the final tool list and deferred setup from candidate tools.

    Fail closed on deferral assembly itself: if tool_search is enabled and
    deferrable candidates exist (MCP tools, or builtins named by ``defer``)
    but no deferred set was recovered, raise rather than silently binding
    their full schemas to the model. Lead-agent authorization is enforced
    separately at runtime by ``SkillToolPolicyMiddleware``; subagents may already
    have applied their static skill policy to ``candidate_tools``.

    Shared by every agent-build path (lead, embedded client, subagent) so they
    all get the same fail-closed guarantee from one place.
    """
    deferred_setup = build_deferred_tool_setup(
        candidate_tools,
        enabled=enabled,
        exclude=exclude,
        defer=defer,
        custom_provider=custom_provider,
        runtime_visible_names=runtime_visible_names,
    )
    if enabled and not deferred_setup.deferred_names and any(_is_deferrable(t, defer=defer, exclude=exclude) for t in candidate_tools):
        raise RuntimeError("tool_search enabled and deferrable candidates exist, but no deferred set was recovered - refusing to bind their schemas (fail-closed).")
    final_tools = list(candidate_tools)
    if deferred_setup.tool_search_tool:
        final_tools.append(deferred_setup.tool_search_tool)
    return final_tools, deferred_setup


def _routing_priority(value: Any) -> int:
    # Produces the typed priority stored in the routing index. McpRoutingMiddleware
    # ._normalize_index re-parses this defensively (it is built to accept arbitrary
    # serialized data), so keep the two coercion rules in sync if either changes.
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _routing_keywords(value: Any) -> list[str]:
    # See _routing_priority: McpRoutingMiddleware._normalize_index re-normalizes
    # keywords defensively; keep both coercion rules aligned.
    if not isinstance(value, list):
        return []
    return [keyword for keyword in (str(item).strip() for item in value) if keyword]


def build_mcp_routing_middleware(
    tools: Iterable[BaseTool],
    deferred_setup: DeferredToolSetup,
    *,
    top_k: int,
) -> "AgentMiddleware | None":
    """Build PR2 auto-promotion middleware from the caller's deferred tools.

    The builder may inspect ``BaseTool.metadata`` at construction time, but the
    returned middleware receives only a flat serializable routing index.
    """
    if deferred_setup.catalog_hash is None or not deferred_setup.deferred_names:
        return None

    routing_index: dict[str, dict[str, Any]] = {}
    for candidate in tools:
        tool_name = getattr(candidate, "name", "")
        if tool_name not in deferred_setup.deferred_names:
            continue
        routing = get_mcp_routing(candidate)
        if routing is None or routing.get("mode") != "prefer":
            continue
        keywords = _routing_keywords(routing.get("keywords"))
        if not keywords:
            continue
        if routing.get("auto_promote_top_k") is not None:
            logger.debug("Ignoring per-tool MCP routing auto_promote_top_k for %s in PR2", tool_name)
        routing_index[str(tool_name)] = {
            "priority": _routing_priority(routing.get("priority", 0)),
            "keywords": keywords,
        }

    if not routing_index:
        return None

    from deerflow.agents.middlewares.mcp_routing_middleware import McpRoutingMiddleware

    return McpRoutingMiddleware(routing_index, deferred_setup.catalog_hash, top_k)


# Prompt rendering


def get_deferred_tools_prompt_section(*, deferred_names: frozenset[str] = frozenset(), directly_bound_names: frozenset[str] = frozenset()) -> str:
    """Generate <available-deferred-tools> and direct-tools guidance.

    Lists deferred names so the agent knows what exists and can use tool_search to
    load them. If directly bound tools exist, explains how to distinguish them
    without claiming that runtime policy left every configured schema visible.
    """
    if not deferred_names:
        return ""
    # Names come verbatim from external MCP servers; escape so a crafted tool
    # name cannot close this block and forge a framework tag. Mirrors
    # get_skill_index_prompt_section.
    names = "\n".join(html.escape(name, quote=False) for name in sorted(deferred_names))
    direct_note = ""
    if any(name for name in directly_bound_names if not name.startswith("__") and name not in {"tool_search", "setup_agent", "update_agent"}):
        direct_note = (
            "\n\n<direct-tool-guidance>\n"
            "`tool_search` can promote only names listed in `<available-deferred-tools>`. Other configured tools may be "
            "directly bound, but runtime policy can hide their schemas. Call a non-deferred tool directly only when its "
            "schema is present in your current tool list. If absent, it is unavailable under the current policy; "
            "`tool_search` cannot activate it, so do not retry the search.\n"
            "</direct-tool-guidance>"
        )
    return f"<available-deferred-tools>\n{names}\n</available-deferred-tools>{direct_note}"


def _format_keyword_list(keywords: list[str]) -> str:
    if len(keywords) == 1:
        return keywords[0]
    return f"{', '.join(keywords[:-1])}, or {keywords[-1]}"


def get_mcp_routing_hints_prompt_section(tools: Iterable[BaseTool], *, deferred_names: frozenset[str] = frozenset()) -> str:
    """Render <mcp_routing_hints> from MCP tools carrying routing metadata.

    When tool_search has deferred an MCP tool, the hint must point the model at
    promotion first; otherwise it may try to call a schema that is hidden from
    the bound model request.
    """
    hints: list[tuple[int, str, list[str]]] = []
    for candidate in tools:
        routing = get_mcp_routing(candidate)
        if routing is None or routing.get("mode") != "prefer":
            continue
        keywords = routing.get("keywords") or []
        if not keywords:
            continue
        hints.append((int(routing.get("priority", 0)), candidate.name, [html.escape(str(keyword), quote=False) for keyword in keywords]))

    if not hints:
        return ""

    lines = ["<mcp_routing_hints>"]
    for priority, tool_name, keywords in sorted(hints, key=lambda item: (-item[0], item[1])):
        # tool_name comes verbatim from the external MCP server; escape at render
        # (keep the raw name for the deferred_names membership check above).
        esc_name = html.escape(tool_name, quote=False)
        lines.append(f"When the user's request involves {_format_keyword_list(keywords)}:")
        if tool_name in deferred_names:
            lines.append(f"  use `tool_search` to fetch `{esc_name}`, then prefer that MCP tool.")
        else:
            lines.append(f"  prefer the `{esc_name}` tool.")
    lines.append("</mcp_routing_hints>")
    return "\n".join(lines)
