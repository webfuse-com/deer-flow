"""Contracts and validation helpers for operator supplied tool discovery.

The harness deliberately knows nothing about Agora, Chronos, or any other
catalog service.  An operator may provide a small callable which returns
serializable capability descriptors.  Discovery only describes capabilities;
execution remains the responsibility of the bound invocation tool.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any, Literal

CustomCapabilityKind = Literal["app_function", "connector_function"]
type CustomDiscoveryProvider = Callable[[str], Any]

_KINDS = frozenset({"app_function", "connector_function"})
_INVOCATION_TOOLS = frozenset({"app_function_call", "connector_call"})
_PROVIDER_STATUSES = frozenset({"ok", "unavailable", "not_configured"})
_REQUIRED_DESCRIPTOR_KEYS = frozenset({"kind", "name", "invocation_tool", "invocation_args", "input_schema", "description", "effect", "version"})


def _is_non_empty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def normalize_custom_descriptor(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    """Validate and copy one provider descriptor.

    Invalid provider records are ignored so one bad app cannot poison local
    MCP discovery.  Values are copied into a fresh JSON-shaped dictionary so a
    provider cannot mutate a result after it has been returned to the model.
    """
    if not isinstance(raw, Mapping):
        return None
    if raw.get("kind") not in _KINDS or not _is_non_empty_string(raw.get("name")):
        return None
    invocation_tool = raw.get("invocation_tool")
    expected_tool = "app_function_call" if raw.get("kind") == "app_function" else "connector_call"
    if invocation_tool != expected_tool or invocation_tool not in _INVOCATION_TOOLS:
        return None
    invocation_args = raw.get("invocation_args")
    if not isinstance(invocation_args, Mapping):
        return None
    # App functions carry app/owner_stack/function; connector functions carry
    # the connector name/function.  Keep the contract intentionally explicit,
    # while allowing additional provider-owned routing fields.
    required_args = {"function", "app", "owner_stack"} if raw.get("kind") == "app_function" else {"name", "function"}
    if any(not _is_non_empty_string(invocation_args.get(key)) for key in required_args):
        return None
    if not isinstance(raw.get("input_schema"), Mapping) or not isinstance(raw.get("description"), str):
        return None
    if raw.get("effect") not in {"read", "write", "destructive", "external-effect"} or not _is_non_empty_string(raw.get("version")):
        return None
    descriptor = {key: deepcopy(raw[key]) for key in _REQUIRED_DESCRIPTOR_KEYS}
    descriptor["invocation_args"] = deepcopy(dict(invocation_args))
    descriptor["input_schema"] = deepcopy(dict(raw["input_schema"]))
    # These are part of the capability contract and remain model-visible.
    for key in ("output_schema", "idempotency"):
        if key in raw:
            descriptor[key] = deepcopy(raw[key])
    try:
        json.dumps(descriptor, ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    return descriptor


def normalize_custom_provider_result(raw: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Normalize the provider response and return descriptors plus status.

    The preferred response is ``{"results": [...], "providers": {...},
    "complete": bool}``, but a plain list remains supported for lightweight
    local providers and tests.
    """
    if isinstance(raw, Mapping):
        records = raw.get("results", [])
        provider_status = raw.get("providers", {})
        complete = raw.get("complete", True)
    else:
        records = raw
        provider_status = {}
        complete = True
    malformed = not isinstance(records, (list, tuple))
    if malformed:
        records = []
    descriptors: list[dict[str, Any]] = []
    for record in records:
        normalized = normalize_custom_descriptor(record)
        if normalized is None:
            malformed = True
        else:
            descriptors.append(normalized)
    statuses = dict(provider_status) if isinstance(provider_status, Mapping) else {}
    return descriptors, {
        "providers": {str(key): (str(value) if str(value) in _PROVIDER_STATUSES else "unavailable") for key, value in sorted(statuses.items(), key=lambda pair: str(pair[0]))},
        "complete": bool(complete) and not malformed,
    }


def invoke_custom_provider(provider: CustomDiscoveryProvider, query: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Invoke a provider and convert failures into explicit partial status."""
    try:
        raw = provider(query)
        # A provider may be async even though the harness tool itself is sync.
        # ``tool_search`` runs sync calls in a worker for async callers; this
        # branch keeps direct sync invocations useful as well.
        if inspect.isawaitable(raw):
            # Providers are deliberately synchronous: async ToolSearch calls
            # already run this function off-loop. Refuse an async provider here
            # rather than blocking an active event loop with thread.join().
            close = getattr(raw, "close", None)
            if callable(close):
                close()
            return [], {"providers": {"custom": "unavailable"}, "complete": False, "error": "async_provider_unsupported"}
        descriptors, status = normalize_custom_provider_result(raw)
        return descriptors, status
    except Exception:
        return [], {"providers": {"custom": "unavailable"}, "complete": False, "error": "provider_failure"}


# Public name used by assembly integrations.  Keep invocation and normalization
# separate above so lightweight providers can reuse either piece in tests.
search_custom_capabilities = invoke_custom_provider
