"""Middleware to detect and break repetitive tool call loops.

P0 safety: prevents the agent from calling the same tool with the same
arguments indefinitely until the recursion limit kills the run.

Detection strategy:
  1. After each model response, hash the tool calls (name + args).
  2. Track recent hashes in a sliding window.
  3. If the same hash appears >= warn_threshold times, queue a
     "you are repeating yourself — wrap up" warning for the current
     thread/run. The warning is **injected at the next model call** (in
     ``wrap_model_call``) as a ``HumanMessage`` appended to the message
     list, *after* all ToolMessage responses to the previous
     AIMessage(tool_calls).
  4. If it appears >= hard_limit times, strip all tool_calls from the
     response so the agent is forced to produce a final text answer.

Why the warning is injected at ``wrap_model_call`` instead of
``after_model``:

  ``after_model`` fires immediately after the model emits an
  ``AIMessage`` that may carry ``tool_calls``. The tools node has not
  run yet, so no matching ``ToolMessage`` exists in the history. Any
  message we add here lands *between* the assistant's tool_calls and
  their responses. OpenAI/Moonshot reject the next request with
  ``"tool_call_ids did not have response messages"`` because their
  validators require the assistant's tool_calls to be followed
  immediately by tool messages. Anthropic also disallows mid-stream
  ``SystemMessage``. By deferring the warning to ``wrap_model_call``,
  every prior ToolMessage is already present in the request's message
  list and the warning is appended at the end — pairing intact, no
  ``AIMessage`` semantics are mutated.

Queued warnings are intentionally transient. If a run ends before the
next model request drains a queued warning, ``after_agent`` drops it
instead of carrying it into a later invocation for the same thread. The
hard-stop path still forces termination when the configured safety limit
is reached.

Stop-reason surfacing (#3875 Phase 2):
  Like the token-budget guard, the loop hard stop does NOT raise — it
  strips ``tool_calls`` so the agent loop terminates naturally with a
  final answer. To let the caller (the subagent executor) distinguish a
  loop-capped completion from a clean one, the run that triggered the hard
  stop is recorded in ``_stop_reason`` and exposed via
  :meth:`consume_stop_reason`. The executor collects that reason alongside
  the token-budget guard's so a loop-capped run surfaces as
  ``completed + loop_capped`` and the lead/ledger can tell it was capped
  without parsing result text.

Result-aware hard-stop gating ([argus] patch #68, extended by #69):
  A Layer-1 hard stop on identical calls is *downgraded to an escalating
  warning* when every repeated tool's most recent result meta says the
  failure is model-recoverable (``partial_success`` no-results bodies, or
  ``error`` results with ``recoverable_by_model=True`` — not_found,
  permission, unknown). Rationale: ToolProgressMiddleware deliberately keeps
  such tools WARNED-not-BLOCKED ("the model can fix this by changing
  strategy"), but this middleware killed the run on the identical retry
  anyway, destroying accumulated research work mid-investigation (observed:
  atlas-nicholas 2026-08, 8 identical ``code_search_logs`` calls answered by
  "No log entries" ended the run with an empty final answer).

  [argus] patch #69 adds the successful-content leg: a ``success`` whose
  content is *near-duplicate* of its own recent successes (word-set Jaccard,
  same helpers/threshold as ToolProgressMiddleware uses) is the same "no new
  information" as a ``no_results`` soft failure and recovers the same way —
  observed: thread 9dc15e99, a paired ``pythia_query`` call re-issued its
  identical pair with the SAME successful chunks ≥5 times and was killed at
  hard_limit 8 with an empty ``[FORCED STOP]`` message. Near-duplicate is
  judged per tool name over the latest result + up to 3 priors;
  ToolProgressMiddleware's default threshold 0.8 / min_words 10 applies.
  A ``success`` with FRESH content still hard-stops (the classic productive
  re-read), and contents too short to judge fall back to the conservative
  stop, same as missing meta.

  The hard stop still fires unchanged when the latest result meta is:
  - ``success`` with distinct content — an identical *re-read* making
    progress; or
  - an unrecoverable error (auth/config/internal/rate_limited) — the retry
    is futile; or
  - stamped ``source=progress_middleware`` — the tool is BLOCKED by
    ToolProgressMiddleware, so hammering it is a genuine loop; or
  - missing entirely (pre-normalization history) — conservative default.

  Downgrades are bounded: after ``recoverable_retry_limit`` identical calls
  (default 24, ~3x a typical hard_limit) the detector stops downgrading and
  hard-stops anyway, bounding the quadratic context cost of a loop that
  ignores escalating warnings. ``no_hard_stop_tools`` is an absolute operator
  opt-out (never hard-stopped, not even at the retry limit; cost is bounded
  by token_budget and run_deadline instead).

  Layer 2 (per-tool frequency) is a volume cap, not an identical-repeat
  detector, so the meta gate does not apply to it — only
  ``no_hard_stop_tools`` exempts there (``tool_freq_overrides`` exists for
  legitimately high-volume tools).
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections import Counter, OrderedDict, defaultdict, deque
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import TYPE_CHECKING, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime

from deerflow.agents.middlewares._bounded_dict import BoundedDict
from deerflow.agents.middlewares.tool_progress_middleware import is_near_duplicate, word_set
from deerflow.agents.middlewares.tool_result_meta import TOOL_META_KEY
from deerflow.sandbox.command_classify import classify_bash_command

# [argus patch #69] Jaccard similarity parameters mirror ToolProgressConfig's
# defaults (jaccard_similarity_threshold=0.8, min_word_count_for_similarity=10)
# so "near-duplicate" means the same thing in both guards.
_SIMILARITY_THRESHOLD = 0.8
_SIMILARITY_MIN_WORDS = 10
_SIMILARITY_HISTORY = 4  # latest result + 3 priors, per tool

if TYPE_CHECKING:
    from deerflow.config.loop_detection_config import LoopDetectionConfig

logger = logging.getLogger(__name__)

# Defaults — can be overridden via constructor
_DEFAULT_WARN_THRESHOLD = 3  # inject warning after 3 identical calls
_DEFAULT_HARD_LIMIT = 5  # force-stop after 5 identical calls
_DEFAULT_WINDOW_SIZE = 20  # track last N tool calls
_DEFAULT_MAX_TRACKED_THREADS = 100  # LRU eviction limit
_DEFAULT_TOOL_FREQ_WARN = 30  # warn after 30 calls to the same tool type
_DEFAULT_TOOL_FREQ_HARD_LIMIT = 50  # force-stop after 50 calls to the same tool type
_DEFAULT_READ_FILE_BUCKET_SIZE = 200  # [argus] read_file line-range bucket (upstream default)
_DEFAULT_RECOVERABLE_RETRY_LIMIT = 24  # [argus patch #68] identical recoverable retries before terminal stop
_MAX_PENDING_WARNINGS_PER_RUN = 4


def _normalize_tool_call_args(raw_args: object) -> tuple[dict, str | None]:
    """Normalize tool call args to a dict plus an optional fallback key.

    Some providers serialize ``args`` as a JSON string instead of a dict.
    We defensively parse those cases so loop detection does not crash while
    still preserving a stable fallback key for non-dict payloads.
    """
    if isinstance(raw_args, dict):
        return raw_args, None

    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}, raw_args

        if isinstance(parsed, dict):
            return parsed, None
        return {}, json.dumps(parsed, sort_keys=True, default=str)

    if raw_args is None:
        return {}, None

    return {}, json.dumps(raw_args, sort_keys=True, default=str)


def _stable_tool_key(name: str, args: dict, fallback_key: str | None, read_file_bucket_size: int = 200) -> str:
    """Derive a stable key from salient args without overfitting to noise.

    ``read_file_bucket_size`` ([argus]) controls how coarsely read_file line
    ranges are bucketed; defaults to 200 (upstream) so bare callers and the
    existing unit tests are unaffected.
    """
    if name == "read_file" and fallback_key is None:
        path = args.get("path") or ""
        start_line = args.get("start_line")
        end_line = args.get("end_line")

        bucket_size = read_file_bucket_size
        try:
            start_line = int(start_line) if start_line is not None else 1
        except (TypeError, ValueError):
            start_line = 1
        try:
            end_line = int(end_line) if end_line is not None else start_line
        except (TypeError, ValueError):
            end_line = start_line

        start_line, end_line = sorted((start_line, end_line))
        bucket_start = max(start_line, 1)
        bucket_end = max(end_line, 1)
        bucket_start = (bucket_start - 1) // bucket_size
        bucket_end = (bucket_end - 1) // bucket_size
        return f"{path}:{bucket_start}-{bucket_end}"

    # write_file / str_replace are content-sensitive: same path may be updated
    # with different payloads during iteration. Using only salient fields (path)
    # can collapse distinct calls, so we hash full args to reduce false positives.
    if name in {"write_file", "str_replace"}:
        if fallback_key is not None:
            return fallback_key
        return json.dumps(args, sort_keys=True, default=str)

    salient_fields = ("path", "url", "query", "command", "pattern", "glob", "cmd")
    stable_args = {field: args[field] for field in salient_fields if args.get(field) is not None}
    if stable_args:
        return json.dumps(stable_args, sort_keys=True, default=str)

    if fallback_key is not None:
        return fallback_key

    return json.dumps(args, sort_keys=True, default=str)


def _hash_tool_calls(tool_calls: list[dict], read_file_bucket_size: int = 200) -> str:
    """Deterministic hash of a set of tool calls (name + stable key).

    This is intended to be order-independent: the same multiset of tool calls
    should always produce the same hash, regardless of their input order.

    ``read_file_bucket_size`` ([argus]) is forwarded to ``_stable_tool_key``;
    it defaults to 200 so bare test callers keep their current behavior.
    """
    # Normalize each tool call to a stable (name, key) structure.
    normalized: list[str] = []
    for tc in tool_calls:
        name = tc.get("name", "")
        args, fallback_key = _normalize_tool_call_args(tc.get("args", {}))
        key = _stable_tool_key(name, args, fallback_key, read_file_bucket_size)

        normalized.append(f"{name}:{key}")

    # Sort so permutations of the same multiset of calls yield the same ordering.
    normalized.sort()
    blob = json.dumps(normalized, sort_keys=True, default=str)
    return hashlib.md5(blob.encode()).hexdigest()[:12]


_WARNING_MSG = "[LOOP DETECTED] You are repeating the same tool calls. Stop calling tools and produce your final answer now. If you cannot complete the task, summarize what you accomplished so far."

# [argus patch #68] Actionable variants: name the tools and the repeat count
# so the model can tell WHICH call is looping and how far it has gone.
_TOOL_LOOP_WARNING_MSG = (
    "[LOOP DETECTED] The same tool call(s) to {tools} have now been repeated {count} times. An identical call will not produce a different result. Vary the arguments meaningfully, or stop calling tools and produce your final answer."
)

_RECOVERABLE_RETRY_MSG = (
    "[LOOP DETECTED] {tools} keeps returning {outcome}, and you have now repeated the identical "
    "call {count} times.{action} Repeating the exact same arguments will not change the outcome. "
    "Change the approach or conclude with the results collected so far."
)

_TOOL_EXEMPT_RETRY_MSG = (
    "[LOOP DETECTED] {tools} has been called with identical arguments {count} times. This tool is exempt from the loop hard stop, but an identical call cannot produce a different result: vary the arguments or conclude with what you have."
)

_TOOL_FREQ_WARNING_MSG = (
    "[LOOP DETECTED] You have called {tool_name} {count} times without producing a final answer. Stop calling tools and produce your final answer now. If you cannot complete the task, summarize what you accomplished so far."
)

_HARD_STOP_MSG = "[FORCED STOP] Repeated tool calls exceeded the safety limit. Producing final answer with results collected so far."

_TOOL_FREQ_HARD_STOP_MSG = "[FORCED STOP] Tool {tool_name} called {count} times — exceeded the per-tool safety limit. Producing final answer with results collected so far."

# [argus patch #82] Subcategory steering messages for Layer-2 frequency detection
_TOOL_FREQ_SUBCATEGORY_WARNING_MSG = "[LOOP DETECTED] Repeated shell micro-reads: {count} inspection-only bash calls. Use read_file / workspace_inspect for file inspection, batch your reads, and move on to implementation."
_TOOL_FREQ_SUBCATEGORY_HARD_STOP_MSG = (
    "[FORCED STOP] Repeated shell micro-reads exceeded the safety limit ({count} inspection-only bash calls). Use read_file / workspace_inspect for file inspection. Producing final answer with results collected so far."
)

_TOOL_FREQ_EXEMPT_MSG = (
    "[LOOP DETECTED] You have called {tool_name} {count} times without producing a final answer. "
    "This tool is exempt from the hard stop, but this volume usually means no new progress is being "
    "made: conclude, or switch to a different approach."
)

# [argus patch #68] Flavor helpers for the downgrade warning: the outcome
# phrase and the concrete advice derive from the tool's own stamped meta.
_OUTCOME_PHRASES: dict[str, str] = {
    "no_results": "no results",
    "not_found": "not-found errors",
    "permission": "permission errors",
}

_ACTION_HINTS: dict[str, str] = {
    "rewrite_query": " Rewrite the query: change the search terms, filters, or scope.",
    "try_alternative": " Try a different tool or a different approach.",
    "summarize": " Summarize what you have gathered and conclude.",
}


def _tool_list(names) -> str:
    return ", ".join(sorted(names))


def _outcome_phrase(meta: dict) -> str:
    if meta.get("status") == "partial_success":
        return "no usable results"
    if meta.get("status") == "success":
        # [argus patch #69] The gate only reaches here for near-duplicate
        # successes (distinct successes keep the hard stop).
        return "near-duplicate results"
    return _OUTCOME_PHRASES.get(meta.get("error_type") or "", "recoverable errors")


def _action_hint(meta: dict) -> str:
    return _ACTION_HINTS.get(meta.get("recommended_next_action") or "", " Change the arguments or the approach.")


def _bash_subcategory(name: str, args: object) -> str | None:
    """[argus patch #82] Compute subcategory name for bash inspection calls."""
    if name != "bash":
        return None
    normalized_args, _ = _normalize_tool_call_args(args)
    cmd = normalized_args.get("command")
    if isinstance(cmd, str) and classify_bash_command(cmd) == "inspection":
        return "bash.inspection"
    return None


class LoopDetectionMiddleware(AgentMiddleware[AgentState]):
    """Detects and breaks repetitive tool call loops.

    Threshold parameters are validated upstream by :class:`LoopDetectionConfig`;
    construct via :meth:`from_config` to ensure values pass Pydantic validation.

    Args:
        warn_threshold: Number of identical tool call sets before injecting
            a warning message. Default: 3.
        hard_limit: Number of identical tool call sets before stripping
            tool_calls entirely. Default: 5.
        window_size: Size of the sliding window for tracking calls.
            Default: 20.
        max_tracked_threads: Maximum number of threads to track before
            evicting the least recently used. Default: 100.
        tool_freq_warn: Maximum number of same-tool-type calls within a
            sliding window of ``_tool_freq_window`` before injecting a
            frequency warning. Catches cross-file read loops that
            hash-based detection misses. Default: 30 (within a window
            of 50).
        tool_freq_hard_limit: Maximum number of same-tool-type calls within
            a sliding window of ``_tool_freq_window`` before forcing a
            stop. Default: 50 (within a window of 50).
        tool_freq_overrides: Per-tool overrides for frequency thresholds,
            keyed by tool name. Each value is a ``(warn, hard_limit)`` tuple
            that replaces ``tool_freq_warn`` / ``tool_freq_hard_limit`` for
            that specific tool. Tools not listed here fall back to the global
            thresholds. Useful for raising limits on intentionally
            high-frequency tools (e.g. ``bash`` in batch pipelines) without
            weakening protection on all other tools. Default: ``None``
            (no overrides).
        no_hard_stop_tools: Tools exempt from hard stops on both layers
            ([argus] patch #68). Warnings still fire, but repeated calls to
            these tools never force-stop the run — cost stays bounded by the
            token budget and run deadline. Default: ``None`` (no exemptions).
        recoverable_retry_limit: Identical-call repetitions tolerated when the
            latest tool result is a model-recoverable soft failure
            ([argus] patch #68); at this count the hard stop fires anyway.
            Default: 24.
    """

    def __init__(
        self,
        warn_threshold: int = _DEFAULT_WARN_THRESHOLD,
        hard_limit: int = _DEFAULT_HARD_LIMIT,
        window_size: int = _DEFAULT_WINDOW_SIZE,
        max_tracked_threads: int = _DEFAULT_MAX_TRACKED_THREADS,
        tool_freq_warn: int = _DEFAULT_TOOL_FREQ_WARN,
        tool_freq_hard_limit: int = _DEFAULT_TOOL_FREQ_HARD_LIMIT,
        tool_freq_overrides: dict[str, tuple[int, int]] | None = None,
        read_file_bucket_size_lines: int = _DEFAULT_READ_FILE_BUCKET_SIZE,
        no_hard_stop_tools: list[str] | None = None,
        recoverable_retry_limit: int = _DEFAULT_RECOVERABLE_RETRY_LIMIT,
    ):
        super().__init__()
        self.warn_threshold = warn_threshold
        self.hard_limit = hard_limit
        self.window_size = window_size
        self.max_tracked_threads = max_tracked_threads
        self.tool_freq_warn = tool_freq_warn
        self.tool_freq_hard_limit = tool_freq_hard_limit
        self.read_file_bucket_size_lines = read_file_bucket_size_lines
        self.recoverable_retry_limit = recoverable_retry_limit
        self._no_hard_stop_tools: frozenset[str] = frozenset(no_hard_stop_tools or ())
        self._tool_freq_overrides: dict[str, tuple[int, int]] = tool_freq_overrides or {}
        # Layer 2's windowed frequency count can never exceed the deque length,
        # so the deque MUST be at least as long as the largest hard limit it is
        # compared against — otherwise the hard-stop branch is dead code. Do NOT
        # reuse Layer 1's ``window_size`` (which is unrelated and defaults below
        # the freq thresholds, e.g. 20 < hard 50); size the frequency window to
        # the largest hard limit in play (global + every per-tool override) so a
        # tight burst can actually reach it while spread-out calls still decay
        # out of the window. Warn thresholds are intentionally excluded: a sane
        # config enforces warn <= hard (covered by sizing to hard), and a misconfig
        # with warn > hard would hard-stop first anyway, so an unreachable warn
        # is harmless and must not inflate the window.
        # [argus patch #82] Note: subcategory keys (e.g. bash.inspection) flow
        # through self._tool_freq_overrides and are automatically covered here too.
        self._tool_freq_window = max(
            self.window_size,
            self.tool_freq_hard_limit,
            *(hard for _, hard in self._tool_freq_overrides.values()),
        )
        self._lock = threading.Lock()
        self._history: OrderedDict[str, list[str]] = OrderedDict()
        self._warned: dict[str, set[str]] = defaultdict(set)
        # Windowed per-tool-type frequency: recent tool names per thread,
        # trimmed to ``window_size`` so the count decays instead of growing
        # monotonically (replaces the old monotonic ``_tool_freq`` integer).
        self._tool_name_history: defaultdict[str, deque[str]] = defaultdict(deque)
        # Per-thread Counter mirroring the deque so freq_count is O(1) instead
        # of scanning the whole window on every tool call. A single high
        # per-tool override (e.g. bash: {hard_limit: 1000}) inflates the window
        # globally, so the scan would cost 1000 per call for every tool; Counter
        # increments on append and decrements on popleft.
        self._tool_name_counter: defaultdict[str, Counter[str]] = defaultdict(Counter)
        # Per-thread set of tool names already warned about in Layer 2, so a
        # frequency warning is enqueued once rather than on every subsequent
        # call. Cleared per name when the windowed count decays back below the
        # warn threshold, mirroring the hash-layer ``_warned`` pruning.
        self._tool_freq_warned: dict[str, set[str]] = defaultdict(set)
        # Per-thread/run queue of warnings to inject at the next model call.
        # Populated by ``after_model`` (detection) and drained by
        # ``wrap_model_call`` (injection); see module docstring.
        self._pending_warnings: dict[tuple[str, str], list[str]] = defaultdict(list)
        self._pending_warning_touch_order: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._max_pending_warning_keys = max(1, self.max_tracked_threads * 2)
        # Stop reason set when a hard-stop fires (#3875 Phase 2). Keyed by run_id
        # (matching ``TokenBudgetMiddleware``) and bounded — the lead agent's
        # middleware instance is long-lived across many runs, so without a cap
        # an entry would accumulate for every looped lead run. Intentionally NOT
        # cleared by ``after_agent``/``_clear_current_run_pending_warnings`` so
        # the subagent executor can consume it after the run returns; ``reset()``
        # still drops it.
        self._stop_reason: BoundedDict[str, str] = BoundedDict(1000)

    @classmethod
    def from_config(cls, config: LoopDetectionConfig) -> LoopDetectionMiddleware:
        """Construct from a Pydantic-validated config, trusting its validation."""
        return cls(
            warn_threshold=config.warn_threshold,
            hard_limit=config.hard_limit,
            window_size=config.window_size,
            max_tracked_threads=config.max_tracked_threads,
            tool_freq_warn=config.tool_freq_warn,
            tool_freq_hard_limit=config.tool_freq_hard_limit,
            tool_freq_overrides={name: (o.warn, o.hard_limit) for name, o in config.tool_freq_overrides.items()},
            read_file_bucket_size_lines=config.read_file_bucket_size_lines,
            no_hard_stop_tools=list(config.no_hard_stop_tools),
            recoverable_retry_limit=config.recoverable_retry_limit,
        )

    def _get_thread_id(self, runtime: Runtime) -> str:
        """Extract thread_id from runtime context for per-thread tracking."""
        thread_id = runtime.context.get("thread_id") if runtime.context else None
        if thread_id:
            return str(thread_id)
        return "default"

    def _get_run_id(self, runtime: Runtime) -> str:
        """Extract run_id from runtime context for per-run warning scoping.

        Keyed by presence, not truthiness: ``SubagentExecutor`` sets
        ``context["run_id"] = self.run_id`` unconditionally (no truthiness
        guard), so an embedded/TUI-dispatched subagent — whose ``run_id`` is
        never assigned per ``AGENTS.md``'s description of the embedded
        ``DeerFlowClient`` — runs with a context that legitimately carries
        ``run_id=None`` (the key is *present*, not absent). The executor
        later reads the stop reason back with the raw attribute,
        ``consume_stop_reason(self.run_id)``, so this must return exactly
        that value (``None`` included) when the key is present, rather than
        collapsing it to a shared fallback indistinguishable from an absent
        key. A truthiness check (``if run_id:``) previously conflated
        "present but None/falsy" with "absent", both mapping to the same
        literal ``"default"`` — so a genuine ``run_id=None`` hard-stop was
        recorded under ``"default"`` here but looked up under ``None`` by
        the executor, silently losing the ``loop_capped`` stop reason.
        Mirrors ``TokenBudgetMiddleware._get_run_id``.
        """
        ctx = getattr(runtime, "context", None)
        if isinstance(ctx, dict) and "run_id" in ctx:
            return ctx["run_id"]
        # Fallback to runtime object ID to prevent collisions across embedded client runs
        return str(id(runtime))

    def consume_stop_reason(self, run_id: str | None) -> str | None:
        """Pop and return the stop reason the hard-stop set for this run.

        Returns ``"loop_capped"`` when a repeated tool-call loop tripped the hard
        stop during the run — the run still completed with a forced final answer
        (the hard stop strips ``tool_calls`` rather than raising). The subagent
        executor calls this after the run returns so a loop-capped completion
        carries ``stop_reason=loop_capped`` to the lead instead of looking like
        a clean ``completed``. Mirrors ``TokenBudgetMiddleware.consume_stop_reason``;
        popping keeps the dict from accumulating on a reused instance.
        """
        with self._lock:
            return self._stop_reason.pop(run_id, None)

    def _pending_key(self, runtime: Runtime) -> tuple[str, str]:
        """Return the pending-warning key for the current thread/run."""
        return self._get_thread_id(runtime), self._get_run_id(runtime)

    def _evict_if_needed(self) -> None:
        """Evict least recently used threads if over the limit.

        Must be called while holding self._lock.
        """
        while len(self._history) > self.max_tracked_threads:
            evicted_id, _ = self._history.popitem(last=False)
            self._warned.pop(evicted_id, None)
            self._tool_name_history.pop(evicted_id, None)
            self._tool_name_counter.pop(evicted_id, None)
            self._tool_freq_warned.pop(evicted_id, None)
            for key in list(self._pending_warnings):
                if key[0] == evicted_id:
                    self._drop_pending_warning_key_locked(key)
            logger.debug("Evicted loop tracking for thread %s (LRU)", evicted_id)

    def _drop_pending_warning_key_locked(self, key: tuple[str, str]) -> None:
        """Drop all pending-warning bookkeeping for one thread/run key.

        Must be called while holding self._lock.
        """
        self._pending_warnings.pop(key, None)
        self._pending_warning_touch_order.pop(key, None)

    def _touch_pending_warning_key_locked(self, key: tuple[str, str]) -> None:
        """Mark a pending-warning key as recently used.

        Must be called while holding self._lock.
        """
        self._pending_warning_touch_order[key] = None
        self._pending_warning_touch_order.move_to_end(key)

    def _prune_pending_warning_state_locked(self, protected_key: tuple[str, str]) -> None:
        """Cap pending-warning state across abnormal or concurrent runs.

        Must be called while holding self._lock.
        """
        overflow = len(self._pending_warning_touch_order) - self._max_pending_warning_keys
        if overflow <= 0:
            return

        candidates = [key for key in self._pending_warning_touch_order if key != protected_key]
        for key in candidates[:overflow]:
            self._drop_pending_warning_key_locked(key)

    def _queue_pending_warning(self, runtime: Runtime, warning: str) -> None:
        """Queue one transient warning for the current thread/run with caps."""
        pending_key = self._pending_key(runtime)
        with self._lock:
            warnings = self._pending_warnings[pending_key]
            if warning not in warnings:
                warnings.append(warning)
            if len(warnings) > _MAX_PENDING_WARNINGS_PER_RUN:
                del warnings[: len(warnings) - _MAX_PENDING_WARNINGS_PER_RUN]
            self._touch_pending_warning_key_locked(pending_key)
            self._prune_pending_warning_state_locked(protected_key=pending_key)

    def _track_and_check(self, state: AgentState, runtime: Runtime) -> tuple[str | None, bool]:
        """Track tool calls and check for loops.

        Two detection layers:
          1. **Hash-based** (existing): catches identical tool call sets.
          2. **Frequency-based** (new): catches the same *tool type* being
             called many times with varying arguments (e.g. ``read_file``
             on 40 different files).

        Returns:
            (warning_message_or_none, should_hard_stop)
        """
        messages = state.get("messages", [])
        if not messages:
            return None, False

        last_msg = messages[-1]
        if getattr(last_msg, "type", None) != "ai":
            return None, False

        tool_calls = getattr(last_msg, "tool_calls", None)
        if not tool_calls:
            return None, False

        thread_id = self._get_thread_id(runtime)
        call_hash = _hash_tool_calls(tool_calls, self.read_file_bucket_size_lines)

        with self._lock:
            # Touch / create entry (move to end for LRU)
            if thread_id in self._history:
                self._history.move_to_end(thread_id)
            else:
                self._history[thread_id] = []
                self._evict_if_needed()

            history = self._history[thread_id]
            history.append(call_hash)
            if len(history) > self.window_size:
                history[:] = history[-self.window_size :]

            warned_hashes = self._warned.get(thread_id)
            if warned_hashes is not None:
                warned_hashes.intersection_update(history)
                if not warned_hashes:
                    self._warned.pop(thread_id, None)

            count = history.count(call_hash)
            tool_names = [tc.get("name", "?") for tc in tool_calls]

            # --- Layer 1: hash-based (identical call sets) ---
            if count >= self.hard_limit:
                # [argus patch #68] Result-aware gating: when every repeated
                # tool's latest result is a model-recoverable soft failure (or
                # the tool is exempt via no_hard_stop_tools), downgrade the
                # hard stop to an escalating warning instead of killing the
                # run mid-investigation. See the module docstring for the
                # full rationale and the conservative fallbacks.
                downgrade_msg = self._hard_stop_downgrade_message(messages, tool_calls, count)
                if downgrade_msg is not None:
                    logger.warning(
                        "Loop hard limit reached — downgraded to warning (recoverable retry or exempt tool)",
                        extra={
                            "thread_id": thread_id,
                            "call_hash": call_hash,
                            "count": count,
                            "tools": tool_names,
                        },
                    )
                    return downgrade_msg, False
                logger.error(
                    "Loop hard limit reached — forcing stop",
                    extra={
                        "thread_id": thread_id,
                        "call_hash": call_hash,
                        "count": count,
                        "tools": tool_names,
                    },
                )
                return _HARD_STOP_MSG, True

            if count >= self.warn_threshold:
                warned = self._warned[thread_id]
                if call_hash not in warned:
                    warned.add(call_hash)
                    logger.warning(
                        "Repetitive tool calls detected — injecting warning",
                        extra={
                            "thread_id": thread_id,
                            "call_hash": call_hash,
                            "count": count,
                            "tools": tool_names,
                        },
                    )
                    # [argus patch #68] Name the tools and the count so the
                    # model can tell WHICH call is looping.
                    return (
                        _TOOL_LOOP_WARNING_MSG.format(tools=_tool_list(set(tool_names)), count=count),
                        False,
                    )

            # --- Layer 2: per-tool-type frequency (windowed) ---
            tool_name_history = self._tool_name_history[thread_id]
            name_counter = self._tool_name_counter[thread_id]
            for tc in tool_calls:
                name = tc.get("name", "")
                if not name:
                    continue
                # Windowed counting: append the name and trim to the frequency
                # window (>= the largest threshold) so the count can reach the
                # warn/hard limits on a tight burst yet still decay for
                # spread-out calls. A mirrored Counter gives O(1) freq_count
                # even when a per-tool override inflates the window globally.
                tool_name_history.append(name)
                name_counter[name] += 1
                while len(tool_name_history) > self._tool_freq_window:
                    old = tool_name_history.popleft()
                    c = name_counter[old] - 1
                    if c <= 0:
                        del name_counter[old]
                    else:
                        name_counter[old] = c
                freq_count = name_counter.get(name, 0)

                if name in self._tool_freq_overrides:
                    eff_warn, eff_hard = self._tool_freq_overrides[name]
                else:
                    eff_warn, eff_hard = self.tool_freq_warn, self.tool_freq_hard_limit

                if freq_count >= eff_hard:
                    # [argus patch #68] no_hard_stop_tools exempts from the
                    # Layer-2 volume cap too: warn, never stop. (The result
                    # meta gate deliberately does NOT apply here — Layer 2
                    # fires on varied arguments, so there is no single
                    # "previous identical result" to consult; per-tool
                    # tool_freq_overrides exist for legitimately chatty tools.)
                    if name in self._no_hard_stop_tools:
                        logger.warning(
                            "Tool frequency hard limit reached — tool exempt via no_hard_stop_tools, downgrading to warning",
                            extra={
                                "thread_id": thread_id,
                                "tool_name": name,
                                "count": freq_count,
                            },
                        )
                        return _TOOL_FREQ_EXEMPT_MSG.format(tool_name=name, count=freq_count), False
                    logger.error(
                        "Tool frequency hard limit reached — forcing stop",
                        extra={
                            "thread_id": thread_id,
                            "tool_name": name,
                            "count": freq_count,
                        },
                    )
                    return _TOOL_FREQ_HARD_STOP_MSG.format(tool_name=name, count=freq_count), True

                if freq_count >= eff_warn:
                    freq_warned = self._tool_freq_warned[thread_id]
                    if name not in freq_warned:
                        freq_warned.add(name)
                        logger.warning(
                            "Tool frequency warning — too many calls to same tool type",
                            extra={
                                "thread_id": thread_id,
                                "tool_name": name,
                                "count": freq_count,
                            },
                        )
                        return _TOOL_FREQ_WARNING_MSG.format(tool_name=name, count=freq_count), False
                else:
                    # Windowed count decayed below the warn threshold; allow a
                    # future burst of this tool to warn again.
                    self._tool_freq_warned[thread_id].discard(name)

                # [argus patch #82] Layer-2 subcategory tracking (e.g. bash.inspection)
                # Gated strictly by key presence in _tool_freq_overrides so default path has zero cost.
                if name == "bash" and "bash.inspection" in self._tool_freq_overrides:
                    subcat = _bash_subcategory(name, tc.get("args"))
                    if subcat:
                        tool_name_history.append(subcat)
                        name_counter[subcat] += 1
                        while len(tool_name_history) > self._tool_freq_window:
                            old = tool_name_history.popleft()
                            c = name_counter[old] - 1
                            if c <= 0:
                                del name_counter[old]
                            else:
                                name_counter[old] = c
                        subcat_freq = name_counter.get(subcat, 0)
                        subcat_warn, subcat_hard = self._tool_freq_overrides[subcat]

                        if subcat_freq >= subcat_hard:
                            if subcat in self._no_hard_stop_tools:
                                logger.warning(
                                    "Tool subcategory frequency hard limit reached — tool exempt via no_hard_stop_tools, downgrading to warning",
                                    extra={
                                        "thread_id": thread_id,
                                        "tool_name": subcat,
                                        "count": subcat_freq,
                                    },
                                )
                                return _TOOL_FREQ_EXEMPT_MSG.format(tool_name=subcat, count=subcat_freq), False
                            logger.error(
                                "Tool subcategory frequency hard limit reached — forcing stop",
                                extra={
                                    "thread_id": thread_id,
                                    "tool_name": subcat,
                                    "count": subcat_freq,
                                },
                            )
                            return _TOOL_FREQ_SUBCATEGORY_HARD_STOP_MSG.format(count=subcat_freq), True

                        if subcat_freq >= subcat_warn:
                            subcat_warned = self._tool_freq_warned[thread_id]
                            if subcat not in subcat_warned:
                                subcat_warned.add(subcat)
                                logger.warning(
                                    "Tool subcategory frequency warning — too many calls to tool subcategory",
                                    extra={
                                        "thread_id": thread_id,
                                        "tool_name": subcat,
                                        "count": subcat_freq,
                                    },
                                )
                                return _TOOL_FREQ_SUBCATEGORY_WARNING_MSG.format(count=subcat_freq), False
                        else:
                            self._tool_freq_warned[thread_id].discard(subcat)

        return None, False

    # ------------------------------------------------------------------
    # [argus patch #68] Result-aware hard-stop gating

    def _hard_stop_downgrade_message(self, messages: list, tool_calls: list[dict], count: int) -> str | None:
        """Return an escalating warning when the Layer-1 hard stop should be
        downgraded instead of killing the run, or ``None`` to keep the stop.

        Downgrade applies when EVERY tool in the repeated call set either:

        - is listed in ``no_hard_stop_tools`` (absolute operator opt-out —
          warns forever, immune even to ``recoverable_retry_limit``), or
        - has a most-recent result whose stamped ``deerflow_tool_meta`` says
          the failure is model-recoverable (``partial_success`` no-results
          bodies, or ``error`` with ``recoverable_by_model=True``), or whose
          content is **near-duplicate** of its own recent successes
          ([argus] patch #69: identical re-reads that return the SAME
          successful chunks, e.g. thread 9dc15e99's pythia_query pair),
          AND the repeat count is still below ``recoverable_retry_limit``.

        Conservative by construction: missing meta, meta stamped by the
        progress middleware (tool BLOCKED), successful results with FRESH
        content (the classic productive re-read), contents too short to
        judge, unknown statuses, and partial-exemption call sets all return
        ``None`` so the hard stop proceeds exactly as before.
        """
        names = {tc.get("name", "") for tc in tool_calls if tc.get("name")}
        if not names:
            return None

        if names <= self._no_hard_stop_tools:
            return _TOOL_EXEMPT_RETRY_MSG.format(tools=_tool_list(names), count=count)

        if count < self.recoverable_retry_limit:
            metas = self._latest_result_meta_per_tool(messages, names)
            contents = self._recent_contents_per_tool(messages, names)
            if metas is not None and all(self._meta_is_recoverable_retry(meta, contents.get(name, [])) for name, meta in metas.items()):
                # Any representative meta flavors the message; sets that got
                # here agree on recoverability, and the action hint is most
                # useful when taken from the (shared) first result.
                representative = next(iter(metas.values()))
                return _RECOVERABLE_RETRY_MSG.format(
                    tools=_tool_list(names),
                    outcome=_outcome_phrase(representative),
                    count=count,
                    action=_action_hint(representative),
                )

        return None

    @staticmethod
    def _latest_result_meta_per_tool(messages: list, names: set[str]) -> dict[str, dict] | None:
        """Most recent ``deerflow_tool_meta`` per tool name.

        Scans backward through the visible message history and keeps, for
        each name, the meta of its most recent ToolMessage. Returns ``None``
        when any tool has no meta-bearing ToolMessage in the history (e.g.
        history compacted away, or a result that skipped normalization) so
        callers fall back to the conservative hard stop.

        ``messages`` is the state channel (ending with the just-emitted
        AIMessage); the most recent ToolMessage per name is the answer to the
        previous identical call, which is exactly the retry the gate is
        judging.
        """
        metas: dict[str, dict] = {}
        for msg in reversed(messages):
            if len(metas) == len(names):
                break
            if getattr(msg, "type", None) != "tool":
                continue
            name = getattr(msg, "name", None)
            if name in names and name not in metas:
                kwargs = getattr(msg, "additional_kwargs", None) or {}
                meta = kwargs.get(TOOL_META_KEY)
                if not isinstance(meta, dict):
                    # Most recent result for this tool carries no meta —
                    # treat as "cannot judge" rather than guessing.
                    return None
                metas[name] = meta
        if len(metas) != len(names):
            return None
        return metas

    @staticmethod
    def _recent_contents_per_tool(messages: list, names: set[str], limit: int = _SIMILARITY_HISTORY) -> dict[str, list[str]]:
        """Most recent ToolMessage contents per tool name, newest first.

        ``contents[name][0]`` is the latest result for that tool; the trailing
        entries are its priors, which the similarity check compares against.
        Bounded by ``limit`` per name; tools with no visible results simply
        have an empty list (the success branch treats that as "cannot judge"
        and keeps the conservative hard stop).
        """
        contents: dict[str, list[str]] = {}
        for msg in reversed(messages):
            if all(len(contents.get(name, [])) >= limit for name in names):
                break
            if getattr(msg, "type", None) != "tool":
                continue
            name = getattr(msg, "name", None)
            if name in names:
                bucket = contents.setdefault(name, [])
                if len(bucket) < limit:
                    value = getattr(msg, "content", "")
                    bucket.append(value if isinstance(value, str) else str(value))
        return contents

    @staticmethod
    def _meta_is_recoverable_retry(meta: dict, contents: list[str]) -> bool:
        """True when the stamped result says the model could fix the failure
        by changing strategy — a nudge case, not a kill case.

        Mirrors ToolProgressMiddleware's own recoverability contract
        (recoverable_by_model=True categories never BLOCK there) so the two
        guards stop disagreeing about the same retry. [argus] patch #69 adds
        the successful-content leg: a ``success`` whose content is
        near-duplicate of its own recent successes is the same "no new
        information" as a ``no_results`` soft failure, so it recovers too;
        ``success`` with fresh content keeps the hard stop (classic re-read).
        """
        if meta.get("source") == "progress_middleware":
            # The tool is BLOCKED by ToolProgressMiddleware. That meta is
            # stamped recoverable=True ("summarize") but hammering a blocked
            # tool is a genuine loop — the block IS the signal to stop.
            return False
        status = meta.get("status")
        if status == "partial_success":
            # no_results-style empty bodies (patch #68 marker set).
            return True
        if status == "success":
            # [argus patch #69] Near-duplicate of its own recent successes is
            # "no new information" — recoverable, same as a soft failure.
            if len(contents) < 2:
                # No prior content to compare against — cannot judge.
                return False
            return is_near_duplicate(
                word_set(contents[0]),
                [word_set(c) for c in contents[1:]],
                _SIMILARITY_THRESHOLD,
                _SIMILARITY_MIN_WORDS,
            )
        if status == "error":
            return bool(meta.get("recoverable_by_model", False))
        # Unknown status — conservative.
        return False

    @staticmethod
    def _append_text(content: str | list | None, text: str) -> str | list:
        """Append *text* to AIMessage content, handling str, list, and None.

        When content is a list of content blocks (e.g. Anthropic thinking mode),
        we append a new ``{"type": "text", ...}`` block instead of concatenating
        a string to a list, which would raise ``TypeError``.
        """
        if content is None:
            return text
        if isinstance(content, list):
            return [*content, {"type": "text", "text": f"\n\n{text}"}]
        if isinstance(content, str):
            return content + f"\n\n{text}"
        # Fallback: coerce unexpected types to str to avoid TypeError
        return str(content) + f"\n\n{text}"

    @staticmethod
    def _build_hard_stop_update(last_msg, content: str | list) -> dict:
        """Clear tool-call metadata so forced-stop messages serialize as plain assistant text."""
        update = {
            "tool_calls": [],
            "content": content,
        }

        additional_kwargs = dict(getattr(last_msg, "additional_kwargs", {}) or {})
        for key in ("tool_calls", "function_call"):
            additional_kwargs.pop(key, None)
        update["additional_kwargs"] = additional_kwargs

        response_metadata = deepcopy(getattr(last_msg, "response_metadata", {}) or {})
        if response_metadata.get("finish_reason") == "tool_calls":
            response_metadata["finish_reason"] = "stop"
        update["response_metadata"] = response_metadata

        return update

    def _apply(self, state: AgentState, runtime: Runtime) -> dict | None:
        warning, hard_stop = self._track_and_check(state, runtime)

        if hard_stop:
            # Record the stop reason so the executor can surface
            # ``stop_reason=loop_capped`` after the run returns (#3875 Phase 2).
            # The hard stop does not raise — it strips tool_calls and lets the
            # run finish with a forced final answer — so without this the caller
            # would see a clean ``completed``. See ``consume_stop_reason``.
            # Written under the lock to match ``TokenBudgetMiddleware``: the lead
            # agent's middleware instance is shared across concurrent Gateway
            # threads, so the bounded-dict write needs the same guard.
            run_id = self._get_run_id(runtime)
            with self._lock:
                self._stop_reason[run_id] = "loop_capped"
            # Also write to runtime.context so the lead worker can read it
            # without needing a reference to this middleware instance (#4176).
            ctx = getattr(runtime, "context", None)
            if isinstance(ctx, dict):
                ctx["stop_reason"] = "loop_capped"
            # Strip tool_calls from the last AIMessage to force text output.
            # Once tool_calls are stripped, the AIMessage no longer requires
            # matching ToolMessage responses, so mutating it in place here
            # is safe for OpenAI/Moonshot pairing validators.
            messages = state.get("messages", [])
            last_msg = messages[-1]
            content = self._append_text(last_msg.content, warning or _HARD_STOP_MSG)
            stripped_msg = last_msg.model_copy(update=self._build_hard_stop_update(last_msg, content))
            return {"messages": [stripped_msg]}

        if warning:
            # Defer injection to the next model call. We must NOT alter the
            # AIMessage(tool_calls=...) here (would put framework words in
            # the model's mouth, polluting downstream consumers like
            # MemoryMiddleware), nor insert a separate non-tool message
            # (would break OpenAI/Moonshot tool-call pairing because the
            # tools node has not produced ToolMessage responses yet). The
            # warning is delivered via ``wrap_model_call`` below.
            self._queue_pending_warning(runtime, warning)
            return None

        return None

    def _clear_other_run_pending_warnings(self, runtime: Runtime) -> None:
        """Drop stale pending warnings for previous runs in this thread."""
        thread_id, current_run_id = self._pending_key(runtime)
        with self._lock:
            for key in list(self._pending_warnings):
                if key[0] == thread_id and key[1] != current_run_id:
                    self._drop_pending_warning_key_locked(key)

    def _clear_current_run_pending_warnings(self, runtime: Runtime) -> None:
        """Drop pending warnings owned by the current thread/run."""
        pending_key = self._pending_key(runtime)
        with self._lock:
            self._drop_pending_warning_key_locked(pending_key)

    @staticmethod
    def _format_warning_message(warnings: list[str]) -> str:
        """Merge pending warnings into one prompt message."""
        deduped = list(dict.fromkeys(warnings))
        return "\n\n".join(deduped)

    @override
    def before_agent(self, state: AgentState, runtime: Runtime) -> dict | None:
        self._clear_other_run_pending_warnings(runtime)
        return None

    @override
    async def abefore_agent(self, state: AgentState, runtime: Runtime) -> dict | None:
        self._clear_other_run_pending_warnings(runtime)
        return None

    @override
    def after_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._apply(state, runtime)

    @override
    async def aafter_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._apply(state, runtime)

    @override
    def after_agent(self, state: AgentState, runtime: Runtime) -> dict | None:
        self._clear_current_run_pending_warnings(runtime)
        return None

    @override
    async def aafter_agent(self, state: AgentState, runtime: Runtime) -> dict | None:
        self._clear_current_run_pending_warnings(runtime)
        return None

    def _drain_pending_warnings(self, runtime: Runtime) -> list[str]:
        """Pop and return all queued warnings for *runtime*'s thread/run."""
        pending_key = self._pending_key(runtime)
        with self._lock:
            warnings = self._pending_warnings.pop(pending_key, [])
            self._pending_warning_touch_order.pop(pending_key, None)
        return warnings

    def _augment_request(self, request: ModelRequest) -> ModelRequest:
        """Append queued loop warnings (if any) to the outgoing message list.

        The warning is placed *after* every existing message, including the
        ToolMessage responses to the previous AIMessage(tool_calls). This
        keeps ``assistant tool_calls -> tool_messages`` pairing intact for
        OpenAI/Moonshot, avoids the Anthropic mid-stream SystemMessage
        restriction (we use HumanMessage), and never mutates an existing
        AIMessage.
        """
        warnings = self._drain_pending_warnings(request.runtime)
        if not warnings:
            return request
        new_messages = [
            *request.messages,
            HumanMessage(content=self._format_warning_message(warnings), name="loop_warning"),
        ]
        return request.override(messages=new_messages)

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        return handler(self._augment_request(request))

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        return await handler(self._augment_request(request))

    def reset(self, thread_id: str | None = None) -> None:
        """Clear tracking state. If thread_id given, clear only that thread."""
        with self._lock:
            if thread_id:
                self._history.pop(thread_id, None)
                self._warned.pop(thread_id, None)
                self._tool_name_history.pop(thread_id, None)
                self._tool_name_counter.pop(thread_id, None)
                self._tool_freq_warned.pop(thread_id, None)
                for key in list(self._pending_warnings):
                    if key[0] == thread_id:
                        self._drop_pending_warning_key_locked(key)
            else:
                self._history.clear()
                self._warned.clear()
                self._tool_name_history.clear()
                self._tool_name_counter.clear()
                self._tool_freq_warned.clear()
                self._pending_warnings.clear()
                self._pending_warning_touch_order.clear()
                self._stop_reason.clear()
