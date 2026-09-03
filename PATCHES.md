# PATCHES.md - Argus carry-list on top of upstream DeerFlow

This fork (`webfuse-com/deer-flow`, branch `argus`) carries a set of patches on
top of `bytedance/deer-flow`. This file is the source of truth for *what* we
carry, *why*, and the concrete condition under which each patch gets deleted,
so fork drift stays legible and every patch has a documented exit.

**Hard rule: any commit that adds, changes, or drops a patch updates this file
in the same commit.** A patch that is not in this file does not exist; a
section whose patch is gone is a bug in this file.

Rebuilt **2026-07-02** from `git log v2.0.0..2df36c99` after the v2.0.0
re-authoring. The pre-v2 numbering was unreliable (two #16s, a mislabeled #10,
a #7/#8 collision between the prompt blocks and the todo middleware). This
file is now canonical; where older documents disagree, the section notes the
alias. Baseline facts:

- Upstream base: tag `v2.0.0` (`7e7f0410`). Fork tip: `2df36c99` (29 commits,
  ~26 logical patches after grouping). Deployed pin: `/opt/argus/VERSIONS.md`.
- Remote layout: `origin` = `webfuse-com/deer-flow` (the fork),
  `bytedance` = upstream. Sync against `bytedance`, never `origin/main`.

To see the live carry-list:

```
git fetch bytedance
git log --oneline v2.0.0..argus        # or bytedance/main..argus after a sync
git diff --stat v2.0.0..argus
```

Class legend: **generic-upstreamable** (fix or feature any deployment wants;
open the PR), **argus-additive** (new files, near-zero merge tax),
**argus-edit** (edits upstream files, the real carry cost),
**config-expressed** (a config field with upstream-default behavior; the code
half is upstreamable, the Argus behavior lives in project config).

## Table of contents

| Patch | Name | Class | Commit(s) |
|---|---|---|---|
| [#1](#patch-1) | aio_sandbox `set +H` | generic-upstreamable | 9b025200 |
| [#2](#patch-2) | sandbox command cap as `SandboxConfig.command_max_chars` | config-expressed | 12362d06 |
| [#3](#patch-3) | loop-detection read_file bucket as config field | config-expressed | 8bf18954 |
| [#4](#patch-4) | checkpointer `AsyncPostgresSaver.aprune` | generic-upstreamable | ef280bbb |
| [#5](#patch-5) | per-event-loop httpx client for ChatOpenAI | generic-upstreamable | 86dcc0b2 |
| [#6](#patch-6) | lead-agent prompt residual blocks | argus-edit | 2262cd29 |
| [#7/#18](#patch-718) | ArgusTodoMiddleware + `uses_planner_pipeline` | argus-additive | dd5f7bfa |
| [#9-chain](#patch-9-chain) | Telegram streaming chain (stage-emoji + HTML + webhook + welcome) | argus-edit | eb379ae8, 152a3d5e |
| [#10](#patch-10) | Telegram channel-aware artifact presenter | argus-edit | eb379ae8 (file), 762b61eb (re-wire) |
| [#11](#patch-11) | PythiaRetrievalMiddleware (company-KB retrieval) | argus-additive | fe44c5ba |
| [#13](#patch-13) | uploads_middleware: steer images to `view_image` | argus-edit | c8d442a7 |
| [#14](#patch-14) | proactive channel notify endpoint | generic-upstreamable | 329bccf6 |
| [#15/#16](#patch-1516) | Caddy SSO trust + CSRF cookie mint | argus-edit | b11b3888 |
| [#20](#patch-20) | view_image vision-describe for non-vision leads | argus-edit | 576f5db7 |
| [#21/#24](#patch-2124) | channel-sender keys into ToolRuntime.context | argus-edit | 1be4c909 (shared with #30) |
| [#22](#patch-22) | Slack thread-context for replies under non-agent posts | argus-edit | 4aae74cd |
| [#23](#patch-23) | Slack progress-ack cleanup | generic-upstreamable | 999f71a8 |
| [coalesce](#patch-coalesce) | split-paste message coalescing | argus-additive | 8a67af3c |
| [#30](#patch-30) | scheduled-playbook fire + per-job agent & memory | argus-additive | 1be4c909, 3cc5491e |
| [#31/#32](#patch-3132) | unattended-silence: empty/filler turns | argus-edit | 488fe077 |
| [#33](#patch-33) | wire `sandbox.network` DNS mode (no host-port publish) | generic-upstreamable | 7e238127 |
| [#34](#patch-34) | unattended-silence: narrated-silence announcements | argus-edit | 8bf85a9c |
| [#35](#patch-35) | landing-galaxy CPU/GPU cost cut | generic-upstreamable | ccf1b69f |
| [#36](#patch-36) | surface `(No response from agent)` on blank final | argus-edit | 89ea4d2f |
| [#37](#patch-37) | retry blank final turn + web display guard | argus-edit | 4a19e4fe |
| [#38](#patch-38) | per-thread debug-sandbox link | argus-additive | 1aad692a, cd03995e, 2df36c99 (merge) |
| [#39](#patch-39) | checkpointer pool bounds | generic-upstreamable | f37b8292 (PR #4 squash-merge) |
| [#40](#patch-40) | Telegram send-path extraction to `_telegram_sender.py` | argus-edit (carry-negative refactor) | a8516b07 (PR #6 squash-merge) |
| [#41](#patch-41) | coerce stringified write_todos arg (planner pipeline) | argus-additive | this PR |
| [#42](#patch-42) | subtask card false-"failed" on transient SSE loading gaps | argus-edit | 68a7fd37 |
| [#43](#patch-43) | per-run allowed-tools from schedule frontmatter | argus-additive | this PR |
| [#44](#patch-44) | unattended-silence: no blank-final retry, wider backstop, no token logging (back-filled) | argus-edit | 8a256f7d |
| [#45](#patch-45) | delivery-report callback for scheduled playbook fires (back-filled) | argus-additive | 27a20421 |
| [#46](#patch-46) | `tool_search.exclude` — deferral opt-out for hot MCP tools (record back-filled) | config-expressed | 9803a9e3 |
| [#47](#patch-47) | per-lead-model summarization overrides (back-filled) | config-expressed | 8e2b51b5, ca0cfe85 |
| [#48](#patch-48) | fail-closed Pythia retrieval ring (back-filled) | argus-edit | 8eec0f98 |
| [#49](#patch-49) | omitted-item index in list-shaped tool-output previews (back-filled) | argus-edit | 7dd2ed96 |
| [#50](#patch-50) | connector call proxy + app overlay-tools proxy (back-filled) | argus-additive | 8364d025 |
| [#51](#patch-51) | inline connector prompts on the playbook fire endpoint (back-filled) | argus-edit | 4bae86f6 |
| [#52](#patch-52) | scheduled fires deliver to root chats only (back-filled) | argus-edit | c10080d6 |
| [#53](#patch-53) | agent-level tool policy (`tool_policy.source: agent`) (back-filled) | config-expressed | 457a984e |
| [#54](#patch-54) | preserve Qwen reasoning_content across turns (PatchedChatQwen) (back-filled) | argus-additive | 77567a8f |
| [#55](#patch-55) | SSO owner gate on single-citizen stacks | argus-edit | this PR |
| [#56](#patch-56) | Artifact "open in new window" button opens in new tab without download | generic-upstreamable | this PR |
| [#57](#patch-57) | Policy-aware guidance for directly bound tools | generic-upstreamable | this PR |
| [#58](#patch-58) | Honor agent-source tool policy at runtime | argus-edit | this PR |
| [#59](#patch-59) | WebUI `onDisconnect: continue` | generic-upstreamable | this PR |
| [#60](#patch-60) | `tool_search.defer` — latency-tiered deferral for builtin tools (+`always_bind` alias) | config-expressed | this PR |
| [#61](#patch-61) | WebUI and server default `max_recursion_limit` bump to 10000 | generic-upstreamable | this PR |
| [#62](#patch-62) | Telegram voice-note inbound via overlay Deepgram STT | argus-additive | this PR |
| [#63](#patch-63) | Summarization must not resurrect answered user turns (upstream backport) | generic-upstreamable | this PR |
| [#64](#patch-64) | Config-gated subagent delegation posture | config-expressed | 09af0482 |
| [#65](#patch-65) | Simplified shared UI + serialized Telegram stage cleanup | argus-edit | this PR |
| [#66](#patch-66) | Salvage partial subagent work on timeout + per-run wall-clock deadline | generic-upstreamable | this PR |
| [#67](#patch-67) | Rejoin in-flight run on WebUI reload + disconnect-safe viewer joins | generic-upstreamable | this PR |
| [#68](#patch-68) | Loop-detection: result-aware hard-stop gating + `no_hard_stop_tools` | argus-edit | this PR |
| [#69](#patch-69) | Loop-detection: near-duplicate SUCCESS downgrades (content Jaccard) | argus-edit | this PR |
| [#70](#patch-70) | surface missing durable run-event storage (back-filled) | generic-upstreamable | 477d652a |
| [#71](#patch-71) | dynamic subagent-type listing in the `task` tool description (was the first #65) | generic-upstreamable | 7a7a3a64, d760d406 |
| [#72](#patch-72) | Atomic edit batching + soft execution-phase budgets | config-expressed | this PR |
| [#73](#patch-73) | Agent execution and context efficiency controls | config-expressed | 71b4e515 |
| [#74](#patch-74) | Recursive completion-ledger compaction handoff | argus-edit | this PR |
| [#75](#patch-75) | Preserve active user request in compaction input | argus-edit | this PR |
| [#76](#reverted-patch-76) | reduce multi-file work-cycle churn (REVERTED 2026-08-31) | argus-edit | d29c414d, fb89651a |
| [#77](#reverted-patch-77) | optional write-file narration (REVERTED 2026-08-31) | argus-edit | 187d7e28, 8f1d49cb |
| [#78](#patch-78) | Merge model profile and runtime constructor kwargs | generic-upstreamable | this PR |
| [#79](#patch-79) | Restore agents gallery navigation; drop in-UI agent creation | argus-edit | this PR |
| [#80](#patch-80) | Sandbox hardening knobs: limits, capabilities, seccomp, no-new-privileges | generic-upstreamable | this PR |
| [#81](#patch-81) | Bash inspection/execution command classification library | argus-additive | this PR |
| [#82](#patch-82) | Bash inspection wiring for ToolProgress streak and loop-detection Layer 2 | config-expressed | this PR |
| [#83](#patch-83) | Truthful meta-classify for wrapper errors; bash.inspection reset semantics | config-expressed | this PR |

Dropped / deferred / not-carried records are at the bottom, followed by the
carry budget ledger.

---

## Patch #1

**Patch #1 - aio_sandbox: prepend `set +H;` to disable bash history expansion**

- Class: generic-upstreamable
- Intent: Commands piping `!`-prefixed tokens (`!DOCTYPE`, heredocs with `!`)
  through the sandbox shell abort with bash "event not found" because history
  expansion is on. Wraps both `exec_command` call sites (initial + the
  upstream `create_session` recovery retry) with `set +H;`. No other side
  effects.
- Files: `backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox.py` (EDITED)
- Tests: `backend/tests/test_aio_sandbox.py` (EDITED upstream file, +cases)
- Delete-when: upstream disables history expansion in the sandbox shell (or
  switches to a non-interactive shell), or the aio-sandbox provider exposes a
  shell-flags hook.
- Upstream status: none (clean PR candidate per FORK-REVIEW; note FORK-REVIEW
  calls this "#2", the pre-v2 PATCHES.md numbering "#1" is used here).

## Patch #2

**Patch #2 - sandbox command length cap as `SandboxConfig.command_max_chars`**

- Class: config-expressed (RE-EXPRESS of the old `_MAX_COMMAND_LENGTH`
  10_000 -> 131_072 constant patch)
- Intent: The 10k default truncates legitimate large commands; Qwen routes
  ~20 KB heredocs (e.g. a self-contained HTML page) through bash in one shot.
  Adds `SandboxConfig.command_max_chars` (default 10000 = upstream, so purely
  additive) read by `SandboxAuditMiddleware` via a new `__init__`; Argus sets
  131072 in project config.
- Files: `backend/packages/harness/deerflow/agents/middlewares/sandbox_audit_middleware.py` (EDITED),
  `.../middlewares/tool_error_handling_middleware.py` (EDITED, 2 lines),
  `backend/packages/harness/deerflow/config/sandbox_config.py` (EDITED)
- Tests: `backend/tests/test_sandbox_audit_middleware.py` (EDITED, +cases)
- Delete-when: upstream ships `command_max_chars` (or an equivalent env/config
  knob) in a released tag; then only the value in project config remains and
  the code patch drops.
- Upstream status: none (designed to be upstreamable: default equals the
  upstream constant).

## Patch #3

**Patch #3 - loop-detection read_file bucket as `LoopDetectionConfig.read_file_bucket_size_lines`**

- Class: config-expressed (RE-EXPRESS of the old 200 -> 50 constant patch)
- Intent: Qwen makes surgical repeated reads of distinct sections of one file;
  with the 200-line bucket those hash as identical calls and trip the loop
  detector. Adds `read_file_bucket_size_lines` (default 200 = upstream),
  threaded from `from_config` into `_hash_tool_calls`/`_stable_tool_key`;
  Argus sets 50 in project config.
- Files: `backend/packages/harness/deerflow/agents/middlewares/loop_detection_middleware.py` (EDITED),
  `backend/packages/harness/deerflow/config/loop_detection_config.py` (EDITED)
- Tests: `backend/tests/test_loop_detection_config.py`,
  `backend/tests/test_loop_detection_middleware.py` (both EDITED, +cases)
- Delete-when: upstream ships the config field, or upstream's per-tool
  frequency overrides (PR #2711, merged) grow the expressiveness to state
  this in config alone.
- Upstream status: none (PR candidate; default equals upstream).

## Patch #4

**Patch #4 - checkpointer: add `AsyncPostgresSaver.aprune`**

- Class: generic-upstreamable
- Intent: `langgraph-checkpoint-postgres` ships `adelete_thread` but no
  `aprune`, so checkpoint history grows unbounded with no in-process prune.
  Adds a monkeypatched `aprune` (keep-latest + delete-all strategies),
  self-activating from `_ensure_postgres_imports()`, idempotent, and a no-op
  if upstream ever ships a native `aprune` (it checks before installing).
  This is what `make prune-threads` on the host relies on.
- Files: `backend/packages/harness/deerflow/runtime/checkpointer/_postgres_aprune.py` (NEW),
  `backend/packages/harness/deerflow/runtime/checkpointer/async_provider.py` (EDITED, +6 import hook)
- Tests: `backend/tests/test_postgres_aprune.py` (NEW)
- Delete-when: upstream ships native `aprune` in `langgraph-checkpoint-postgres`
  >= some release AND the base image consumes that version; confirm the patch
  reports itself a no-op, then drop the file + import.
- Upstream status: none (belongs in `langgraph-checkpoint-postgres`, not
  deer-flow; file it there).

## Patch #5

**Patch #5 - models/factory: per-event-loop httpx client for ChatOpenAI**

- Class: generic-upstreamable (workaround for an upstream-of-upstream bug)
- Intent: langchain-openai caches the async httpx client in a process-global
  lru_cache; LangGraph's loop-per-task model then reuses a client from a
  torn-down loop and streams die with "RuntimeError: Event loop is closed" at
  cleanup. Injects an explicit `http_async_client` keyed on `id(loop)` via a
  `WeakValueDictionary`, only for ChatOpenAI subclasses and only when the
  caller didn't pass one. Load-bearing but fragile: re-run its test after
  every dependency bump.
- Files: `backend/packages/harness/deerflow/models/factory.py` (EDITED, ~60 lines)
- Tests: `backend/tests/test_model_factory.py` (EDITED, +cases)
- Delete-when: `langchain-ai/langchain#35783` is fixed in a released
  langchain-openai that the image consumes, OR DeerFlow stops sharing httpx
  clients across loops.
- Upstream status: issue (langchain-ai/langchain#35783, still open 2026-06);
  subsumed-watch on that issue.

## Patch #6

**Patch #6 - lead_agent/prompt: residual `<file_editing>` + `<debugging_when_stuck>` blocks**

- Class: argus-edit
- Intent: Qwen-specific prompt tuning: read_file before editing your own
  earlier output, avoid heredocs for files you may edit, edit deliverables in
  place, plus the full `<debugging_when_stuck>` instrument-first /
  reduce-surface decision rule for smaller models. The v2.0.0 re-port DROPPED
  the str_replace-over-write_file pillar because upstream now states it
  natively in `<critical_reminders>` (upstream #3195); only the residuals are
  carried. Pre-v2 this was two patches (#6 + #7); the todo-middleware commit
  now claims #7, so the merged prompt patch is #6 alone.
- Files: `backend/packages/harness/deerflow/agents/lead_agent/prompt.py` (EDITED; the most upstream-churned file we touch)
- Tests: `backend/tests/test_lead_agent_prompt.py` (EDITED, +cases)
- Delete-when: never cleanly (Qwen-specific tuning); re-express via a SOUL /
  agent-config prompt-section injection layer if upstream ships one, and check
  each sync whether upstream absorbed more of the guidance (as #3195 did).
  Surface: `lead_agent/prompt.py`.
- Upstream status: subsumed-watch (upstream absorbs pillars piecemeal, #3195
  precedent).

## Patch #7/#18

**Patch #7/#18 - ArgusTodoMiddleware + `AgentConfig.uses_planner_pipeline`**

- Class: argus-additive
- Intent: A planner-aligned `TodoMiddleware` subclass whose prompt defers
  planning judgment to the planner SKILL.md instead of upstream's "do not plan
  simple tasks". Routed by a new `uses_planner_pipeline` flag on `AgentConfig`
  (replaces the old `agent_name == "qwen-local-coder"` match) so glm-planner
  and any planner/critic-pipeline agent gets it; `build_middlewares` loads the
  agent config by name (best-effort) to read the flag. Alias note: the pre-v2
  PATCHES.md numbered the todo middleware #8; the carried commit says
  "#7 + #18" and that is canonical now.
- Files: `backend/packages/harness/deerflow/agents/middlewares/argus_todo_middleware.py` (NEW),
  `backend/packages/harness/deerflow/agents/lead_agent/agent.py` (EDITED, wiring),
  `backend/packages/harness/deerflow/config/agents_config.py` (EDITED, flag)
- Tests: `backend/tests/test_argus_todo_middleware.py` (NEW),
  `backend/tests/test_lead_agent_model_resolution.py` (EDITED, factory stubs)
- Delete-when: upstream's TodoMiddleware gains planner-alignment, or upstream
  #3809 (pluggable middleware builder) lands in a shipped tag with a
  `middlewares` config key accepted; then the class becomes a local plugin and
  the `agent.py` wiring drops.
- Upstream status: subsumed-watch (upstream #3809).

## Patch #9-chain

**Patch #9/#26a/#27/#28/#29 - Telegram streaming chain: stage-emoji + HTML + webhook + welcome**

- Class: argus-edit (the single biggest carry: ~90% of our upstream-file
  edited lines live in `app/channels/`)
- Intent: Telegram as a first-class Argus channel. Markdown -> Telegram-native
  HTML with a tag-safe 4096 chunker (`_telegram_format.py`); animated
  stage-emoji progress indicator instead of upstream's edit-in-place partial
  text (manager `_stage_from_chunk` derives received/thinking/planning/
  searching/working from the langgraph stream; suppressed on unattended
  turns); stage sends are fire-and-forget so Telegram API latency never stalls
  the stream loop (#26a/#27); webhook mode `POST /webhooks/telegram` kills the
  0-10s polling delay (#28, config-gated, polling stays as fallback; honor both
  `webhook:` and `webhook_mode:`, and `initialize()`/`start()` the PTB app in
  `_register_webhook_route` — without those, Atlas `webhook: true` falls through
  to polling and inbound `get_file` raises NetworkError while captions still
  arrive); grafts
  upstream's orthogonal v2 features (user-owned connection binding,
  bot-username stripping, /bootstrap + unknown-slash routing) and drops
  upstream's superseded text-stream helpers. The follow-up commit 152a3d5e
  restores the #28 sub-commits lost in the port: `/webhooks/` in auth
  `_PUBLIC_PATH_PREFIXES` + CSRF exemption (without them every webhook push
  403'd before reaching the route's own secret-token check). Numbering note:
  the commit subject also lists #10 and #24; those halves have their own
  sections. "#9" here is the telegram-core re-port label, NOT the dead
  langgraph_auth patch that older notes also called #9.
- Files: `backend/app/channels/_telegram_format.py` (NEW),
  `backend/app/channels/_telegram_sender.py` (NEW - since patch #40 hosts the
  telegram-side send path; state stays on the channel, shims in telegram.py),
  `backend/app/channels/telegram.py` (EDITED - since #40 a thin carrier:
  send/_send_running_reply/_send_running_reply_safe shims + webhook (#28) +
  welcome/lock (#17); upstream's superseded stream helpers restored verbatim
  as unreachable code),
  `backend/app/channels/manager.py` (EDITED),
  `backend/app/channels/message_bus.py` (EDITED, `OutboundMessage.progress_stage`),
  `backend/app/gateway/auth_middleware.py` (EDITED, 152a3d5e),
  `backend/app/gateway/csrf_middleware.py` (EDITED, 152a3d5e)
- Tests: `backend/tests/test_telegram_format.py` (NEW),
  `backend/tests/test_telegram_send.py` (NEW),
  `backend/tests/test_channels.py` (EDITED),
  `backend/tests/test_csrf_middleware.py` (EDITED, webhook-exemption regression tests).
  Carry-repair: `test_channels.py` TestTelegramInboundMessages locks `webhook:`
  vs `webhook_mode:`, initialize/start on the webhook route, no polling thread
  in webhook start, and gateway-loop `receive_file` when `_tg_loop` is unset.
- Delete-when: upstream's Telegram channel gains HTML/markdown rendering, a
  removable working-indicator/stage hook, and per-channel formatter extension
  points; realistically never as a whole. The exit path is FORK-REVIEW lever
  #1: upstream the design or move it behind a cleaner extension point. Since
  patch #40, upstream churn inside its own stream helpers merges clean (they
  are restored verbatim, unreachable); the remaining reconciliation surface on
  each sync is the three shim bodies, the webhook/welcome edits, and any
  upstream change to send()'s signature or OutboundMessage fields (locked by
  `test_telegram_sender_seam.py`).
- Upstream status: none.
- See also: patch #40 (send-path extraction).

## Patch #10

**Patch #10 - Telegram channel-aware artifact presenter**

- Class: argus-edit (new file + a small, historically fragile manager seam)
- Intent: For Telegram, `present_files` HTML/SVG reports become viewable links
  to the per-stack `/f/` nginx fileserver (raw file suppressed) instead of
  unrenderable downloads; other binaries are linked + attached; orphan
  viewable artifacts (mtime-detected, `_ORPHAN_PRESENT_EXTS` only) are
  auto-presented. **Sync trap, do not repeat:** the v2.0.0 rebase note
  recorded #10 as "supports_streaming override, dropped as subsumed". That was
  a mislabel. #10 is THIS presenter and was NOT subsumed: the rebase carried
  `_artifact_presenter.py` but upstream's owner-scoping refactor (#3579) had
  rewritten `_prepare_artifact_delivery` and silently dropped the Telegram
  hand-off, regressing every atlas stack to bare-filename delivery. Re-wired
  in 762b61eb: `_prepare_artifact_delivery` regains its `channel_name` param
  beside the #3579 `user_id` kwarg, both ChannelManager call sites pass
  `msg.channel_name`. Do not treat #10 as obsolete on the next sync.
- Files: `backend/app/channels/_artifact_presenter.py` (NEW, via eb379ae8),
  `backend/app/channels/manager.py` (EDITED, the hand-off seam, 762b61eb)
- Tests: `backend/tests/test_artifact_presenter.py` (NEW; includes 2
  manager-seam regression tests added in 762b61eb precisely so a future
  upstream rewrite of `_prepare_artifact_delivery` fails loudly)
- Delete-when: upstream grows a per-channel artifact-presentation hook in the
  delivery path; the `/f/`-link behavior itself is Argus product behavior
  (depends on our per-stack fileserver) and would be re-expressed against that
  hook, not deleted.
- Upstream status: subsumed-watch (upstream #3579 rewrote this seam once
  already and cost us the wiring; watch it every sync).

## Patch #11

**Patch #11 - PythiaRetrievalMiddleware: deterministic company-KB retrieval**

- Class: argus-additive
- Intent: On the first model call of a turn, POST kb-api `/{project}/answer`
  and inject the returned cited context blocks via
  `wrap_model_call` + `request.override()` (non-persisting: the
  `[pythia-kb-context]` block reaches the model for that call only and never
  renders in UI/exports/checkpoints). Thin client: routing lives server-side
  in kb-api. Adds `AgentConfig.pythia_ring` (a server-side ring CEILING
  enforced by kb-api) and a gateway-signed `caller_token`
  (`PYTHIA_CALLER_SIGNING_SECRET`, byte-identical format to kb-api's
  verifier). Gated by `PYTHIA_ROUTER_INJECT` (alias
  `PYTHIA_RETRIEVAL_ENABLED`), so it degrades cleanly on stacks that don't
  use it.
- Files: `backend/packages/harness/deerflow/agents/middlewares/pythia_retrieval_middleware.py` (NEW),
  `backend/packages/harness/deerflow/agents/lead_agent/agent.py` (EDITED, wiring),
  `backend/packages/harness/deerflow/config/agents_config.py` (EDITED, `pythia_ring`)
- Tests: `backend/tests/test_pythia_retrieval_middleware.py` (NEW; pins ring
  gating, the token contract, context formatting, per-turn dedup)
- Delete-when: never - core product behavior (Pythia company-KB answers).
  Re-express the `build_middlewares` wiring as a plugin registration when
  upstream #3809 ships; drop only if DeerFlow gains a first-class pre-model
  retrieval hook that can call an external router.
- Upstream status: none (wiring: subsumed-watch on #3809).

## Patch #13

**Patch #13 - uploads_middleware: steer uploaded images to `view_image`**

- Class: argus-edit
- Intent: The frontend uploads images by path, never as inline `image_url`
  blocks, so `view_image` is the only route to the pixels; upstream's
  read_file/grep/glob guidance left vision-capable models unable to see an
  uploaded screenshot. Emits per-image `view_image(image_path=...)` guidance
  in `_format_file_entry` and suppresses the trailing doc-search block when
  every uploaded file is an image. Non-image entries byte-for-byte unchanged.
- Files: `backend/packages/harness/deerflow/agents/middlewares/uploads_middleware.py` (EDITED)
- Tests: `backend/tests/test_uploads_middleware_core_logic.py` (EDITED, +cases)
- Delete-when: the frontend inlines image uploads as `image_url` content
  blocks (no tool round-trip needed), OR upstream's uploads guidance becomes
  image-aware.
- Upstream status: none.

## Patch #14

**Patch #14 - gateway/routers/channels: proactive notify endpoint**

- Class: generic-upstreamable
- Intent: `POST /api/channels/{name}/notify` publishes a synthetic
  `InboundMessage` onto the channel bus so scheduled jobs (Atlas briefing,
  Chronos turns) ride the full real-user channel pipeline: thread mapping,
  agent turn, formatting, artifact delivery, reply threading. Guarded by the
  internal service token specifically; an SSO session is NOT accepted because
  the caller chooses `chat_id`/`user_id` (impersonation otherwise). v2.0.0
  already widened the router imports, so only the endpoint body is appended.
- Files: `backend/app/gateway/routers/channels.py` (EDITED, additive endpoint)
- Tests: `backend/tests/test_channel_notify.py` (NEW)
- Delete-when: upstream grows a proactive/outbound message API for channels
  (a send-without-inbound surface in `app/channels/`).
- Upstream status: none (PR candidate per FORK-REVIEW).

## Patch #15/#16

**Patch #15/#16 - trust Caddy SSO identity + mint the CSRF cookie for SSO sessions**

- Class: argus-edit
- Intent: #15: the gateway authenticates off the edge-verified `X-Auth-Email`
  header, trusted ONLY when the request carries the matching
  `X-Auth-Proxy-Secret` (constant-time compare vs `DEER_FLOW_SSO_PROXY_SECRET`,
  fail-closed when unset), auto-provisioning the user by email as an `elif`
  branch in v2.0.0's internal/access_token/auth_disabled/401 ladder; `/me`
  prefers the middleware-resolved user; the frontend SSR `/me` fetch forwards
  the SSO headers. Kills the second login behind Caddy and unifies the
  identity #11's caller tokens use. #16: the double-submit `csrf_token` cookie
  was only minted on local-login POSTs, which SSO citizens never make; a
  trusted-SSO request with no cookie is treated as first contact - skip the
  double-submit rejection for that one request and mint the cookie. Same
  proxy-secret gate, so both branches are inert without the secret.
- Files: `backend/app/gateway/sso_auth.py` (NEW),
  `backend/app/gateway/auth_middleware.py` (EDITED),
  `backend/app/gateway/csrf_middleware.py` (EDITED),
  `backend/app/gateway/deps.py` (EDITED, `resolve_or_provision_sso_user`),
  `backend/app/gateway/routers/auth.py` (EDITED),
  `frontend/src/core/auth/server.ts` (EDITED)
- Tests: `backend/tests/test_sso_auth.py` (NEW, 5 cases)
- Delete-when: upstream gains a first-class reverse-proxy-auth / trusted
  identity-header mode covering both the gateway and the SSR auth check; #16
  falls automatically with #15. Until then this is core deployment behavior
  for every SSO-fronted stack. Surface: the gateway auth ladder (rewritten
  once already in v2.0.0; expect to re-place the branch each major sync).
- Upstream status: none.

## Patch #55

**Patch #55 - SSO owner gate: a verified identity is not an authorized one**

- Class: argus-edit
- Intent: patch #15 made the gateway trust the edge's `X-Auth-Email` and
  auto-provision the user by email. That is right for authentication and wrong
  for authorization on a **single-citizen** stack, where the run executes AS the
  owner: `argus_caller_token` signs the knowledge-ring caller token with the
  stack's `PYTHIA_CALLER_EMAIL`, the gateway env holds that citizen's
  Gmail/GitHub/Asana credentials, and the sandbox mounts their personal
  knowledge ring read-write. So a different citizen arriving with a perfectly
  valid SSO header was silently given an account and, through the agent, the
  owner's mail, notes and rings. `sso_email_allowed()` compares the verified
  email against the stack owner and refuses **before**
  `resolve_or_provision_sso_user`, so a rejected visitor leaves no user row.
  Exempt by name: projects whose `ARGUS_PROJECT` is not `atlas-*` (e.g. `pythia`)
  are legitimately multi-citizen and carry no owner. Exempt by path: the
  shared-app API prefixes (`/api/apps/`, `/api/connectors/`, `/api/transformers/`)
  stay open to any authenticated citizen, because apps are shareable by design
  and the Argus edge proxies them from the `apps-<owner>` origin into the
  owner's gateway. An `atlas-*` stack that declares no owner fails **closed**
  (SSO trust off, local login remains as break-glass) rather than degrading to
  "anyone authenticated". Returns 403, not 401: the SPA keys its login redirect
  off the status, so 401 would bounce a rejected citizen through Google and back
  into the same refusal forever.
- Deployment note: this is the SECOND layer. The primary control is the Argus
  edge, which 403s a non-owner before the request reaches this gateway. This
  patch covers what the edge cannot: a direct `argus-net`/tailnet caller holding
  the proxy secret, and any future edge misconfiguration.
- Files: `backend/app/gateway/sso_auth.py` (EDITED),
  `backend/app/gateway/auth_middleware.py` (EDITED)
- Tests: `backend/tests/test_sso_auth.py` (11 new cases),
  `backend/tests/test_auth_middleware.py` (2 new cases)
- Delete-when: upstream grows a first-class tenancy model where a deployment can
  declare "this instance belongs to exactly one principal" and the auth layer
  enforces it. Falls together with #15, which it constrains. Surface: the same
  auth ladder #15 edits, so expect to re-place both on a major sync.
- Upstream status: none. Arguably upstreamable as an opt-in
  `AuthConfig.sso_single_owner` field; not offered yet because the guest-path
  allowlist is Argus-shaped (it names our app-tier routes).

## Patch #56

**Patch #56 - Artifact "open in new window" button opens in new tab without forced download**

- Class: generic-upstreamable
- Intent: In the artifact detail view, the "open in new window" action previously called
  `window.open(url, "_blank", "noopener,noreferrer")` followed by `if (w) w.opener = null;`.
  Passing the windowFeatures argument string caused browsers to open the link in a popup/new window
  or trigger download handling instead of opening a standard browser tab. Removing the features
  argument lets `window.open(url, "_blank")` cleanly open the artifact inline in a new browser tab.
- Files: `frontend/src/components/workspace/artifacts/artifact-file-detail.tsx` (EDITED)
- Tests: `frontend/tests/unit/core/artifacts/` (Rstest suite passes)
- Delete-when: upstream adopts standard new-tab opening for artifacts.
- Upstream status: clean generic PR candidate.

## Patch #57

**Patch #57 - Policy-aware guidance for directly bound tools**

- Class: generic-upstreamable
- Intent: Build-time tool assembly knows which tools are configured as directly
  bound, but `SkillToolPolicyMiddleware` can hide any of those schemas from one
  model call. The previous prompt and `tool_search` no-match response called all
  configured direct tools "ALREADY active" and told the model to call them
  directly. That was false after policy filtering and made Kimi repeatedly search
  for `bash`, then claim the runtime would not let it invoke Bash. Guidance now
  uses the model's current tool list as the authority: direct-call a non-deferred
  tool only when its schema is present; if absent, policy made it unavailable and
  `tool_search` cannot restore it. The prompt no longer enumerates every configured
  direct tool, reducing noise and avoiding claims about policy-hidden schemas.
- Files: `backend/packages/harness/deerflow/tools/builtins/tool_search.py` (EDITED)
- Tests: `backend/tests/test_deferred_setup.py`,
  `backend/tests/test_tool_search.py` (EDITED)
- Delete-when: upstream makes direct-tool guidance derive from the post-policy
  model request, or otherwise distinguishes configured binding from current schema
  visibility.
- Upstream status: clean generic PR candidate.

## Patch #58

**Patch #58 - Honor agent-source tool policy at runtime**

- Class: argus-edit
- Intent: Patch #53 makes `tool_policy.source: agent` authoritative: agent
  `allowed_tools` plus a schedule's run-scoped list gate tools, while skill
  `allowed-tools` is documentation only. The upstream runtime
  `SkillToolPolicyMiddleware` was still installed unconditionally, so a skill
  loaded in an earlier turn persisted in `skill_context` and silently removed
  tools from later model calls. In the production failure, loading
  `ticket-management` removed `bash` from Kimi's schema despite Atlas having an
  unrestricted agent policy. The lead now installs runtime skill filtering only
  for the default `skills` source and applies the agent/schedule ceiling before
  authorization and deferred-tool assembly under the `agent` source.
- Files: `backend/packages/harness/deerflow/agents/lead_agent/agent.py` (EDITED)
- Tests: `backend/tests/test_lead_agent_model_resolution.py`,
  `backend/tests/test_tool_policy_agent_source.py` (EDITED)
- Delete-when: upstream supports an agent-level tool-policy source with the same
  tri-state and run-scoped schedule semantics.
- Upstream status: none; the policy source is Argus-specific configuration.

## Patch #59

**Patch #59 - WebUI `onDisconnect: continue`**

- Class: generic-upstreamable
- Intent: Web `thread.submit` never sent `onDisconnect`, so Gateway used
  `on_disconnect=cancel`. Unmounting `useStream` (thread switch, tab close)
  dropped SSE and `sse_consumer` cancelled the worker. Both submit paths now
  send `WEB_THREAD_SUBMIT_STREAM_OPTIONS` (`streamResumable: true`,
  `onDisconnect: "continue"`). Stop still `thread.stop()` → cancel. Gateway
  HTTP default stays `cancel`. Sandbox `idle_timeout` is independent.
- Files: `frontend/src/core/threads/submit-stream-options.ts` (NEW),
  `frontend/src/core/threads/hooks.ts` (EDITED),
  `frontend/src/AGENTS.md` (EDITED),
  `backend/app/gateway/AGENTS.md` (EDITED),
  `backend/docs/STREAMING.md` (EDITED)
- Tests: `frontend/tests/unit/core/threads/submit-stream-options.test.ts` (NEW),
  `frontend/tests/unit/core/api/stream-mode.test.ts` (EDITED; sanitizer must
  keep `onDisconnect` while still stripping `streamResumable`)
- Delete-when: upstream WebUI sends `onDisconnect: continue` on every submit
  path, or Gateway defaults to continue for stream clients.
- Upstream status: clean generic PR candidate.


## Patch #60

**Patch #60 - `tool_search.defer`: latency-tiered deferral for builtin tools (+ `always_bind` alias)**

- Class: config-expressed (default `defer: []` keeps upstream behavior
  byte-identical; `always_bind` is a pure alias for `exclude`).
- Intent: Deferral eligibility was hardcoded to MCP-tagged tools, so builtin
  suites whose intrinsic latency dwarfs one promotion round-trip (the 9
  `browser_*` tools: ~1.1K tokens of schema on every model call vs a
  seconds-long browser spin-up) could not be moved to the discoverable path.
  `tool_search.defer` (fnmatch on final names) extends deferral to non-MCP
  tools: name-only in `<available-deferred-tools>` until promoted, still
  graph-registered so in-process execution is unchanged. `always_bind`/
  `exclude` wins over `defer` for the same name. Tagging the builtin objects
  was rejected: they are process-global singletons and metadata mutation
  leaks across agent builds. Documented limitation (pinned by test): keyword
  auto-promotion never covers deferred builtins — routing metadata exists
  only for MCP servers — so they promote via explicit `tool_search` only.
- Files: `backend/packages/harness/deerflow/config/tool_search_config.py`
  (EDITED: `defer` field, `always_bind` alias),
  `backend/packages/harness/deerflow/tools/builtins/tool_search.py` (EDITED:
  shared `_is_deferrable()` used by setup AND the fail-closed guard so the
  two predicates cannot drift), the four assemble call sites
  (`agents/lead_agent/agent.py` x2, `client.py`, `subagents/executor.py`),
  `config.example.yaml`, `backend/README.md`.
- Tests: `backend/tests/test_deferred_setup.py` (+TestDeferPatterns: match,
  always_bind-beats-defer, defer-without-MCP, guard parity, config alias),
  `backend/tests/test_deferred_filter_middleware.py` (+deferred-builtin
  hide/promote), `backend/tests/test_mcp_routing_auto_promote.py`
  (+deferred-builtin-is-NOT-auto-promoted pin).
- Delete-when: upstream ships a source-agnostic deferral eligibility config
  (or defers builtins natively); then only the values in project config
  remain.
- Upstream status: clean generic PR candidate (nothing argus-specific in the
  mechanism; the argus-specific part is the `browser_*` value in stack
  config).

## Patch #66

**Patch #66 - Salvage partial subagent work on timeout, and bound a run by wall clock**

- Class: generic-upstreamable (nothing argus-specific; both halves are gaps in
  upstream's own guard model).
- Why: atlas-nicholas thread `5a3be3f1` (2026-08-26) spent **60 minutes,
  36.6M tokens and 115 LLM calls** on one user turn and delivered nothing.
  Two independent defects, both upstream:

  1. **Timeout discarded all work.** Six `architect` subagents were dispatched;
     three hit `timeout_seconds` at 600s. `executor.py`'s `FuturesTimeoutError`
     branch stamped `TIMED_OUT` and cancelled without ever reading
     `result.ai_messages`, which was in scope and held ~140 captured messages.
     The lead received the string `"Task timed out. Error: Execution timed out
     after 600 seconds"` and nothing else, then `cleanup_background_task()`
     freed the buffer. Roughly 28M subagent tokens bought zero information.
     The salvage precedent already existed 200 lines above in the same file:
     `GraphRecursionError` recovers the trailing partial and reports
     `COMPLETED` + `stop_reason="turn_capped"`.
  2. **Nothing watched the clock.** `recursion_limit: 10000` was 20% consumed.
     `TokenBudgetMiddleware` was disabled on that stack for a sound reason
     (cumulative token counting grows ~quadratically with turns, so a cap tight
     enough to catch a runaway truncates legitimate deep work mid-artifact).
     `LoopDetectionMiddleware` is call-shaped: Layer 1 hashes tool *arguments*,
     so re-researching the same question with varied commands never repeats a
     hash, and Layer 2 is a per-tool volume cap. Nothing measured elapsed time.

- What it does:
  - **Timeout salvage.** `_last_assistant_text_from_captured()` scans
    `result.ai_messages` backwards for the last assistant turn with real text.
    Found -> `COMPLETED` + `stop_reason="time_capped"` carrying the partial;
    not found -> `TIMED_OUT` exactly as before. Unlike the recursion salvage,
    which inspects only the final `AIMessage`, this skips trailing
    tool-call-only turns, because a wall-clock kill lands mid-tool-call almost
    every time. `content: None` is skipped explicitly:
    `message_content_to_text` falls through to `str(content)` and would
    otherwise salvage the literal `"None"`.
  - **`time_capped` stop reason.** Added to `SubagentStopReasonValue`,
    `SUBAGENT_STOP_REASON_VALUES` and `_STOP_REASON_LABELS`
    ("wall-clock timeout"); contract fixture bumped to **v3**. No change to
    `_RESULT_BEARING_STATUSES` is needed: a capped-but-usable run is already
    modelled as `completed` + a stop reason, so `format_subagent_result_message`
    renders `"Task Succeeded (capped: wall-clock timeout). Result: ..."` with no
    consumer changes.
  - **`RunDeadlineMiddleware`** (`run_limits:` config, disabled by default) is
    `TokenBudgetMiddleware` with a clock instead of a counter, installed
    immediately after it in the lead chain. Warns once at `warn_at_seconds`
    via the deferred `wrap_model_call` injection (so
    `AIMessage(tool_calls)` -> `ToolMessage` pairing survives), then at
    `wall_clock_seconds` strips `tool_calls`, appends a stop notice and stamps
    `stop_reason="time_capped"` on both `consume_stop_reason` and
    `runtime.context`. It never raises: the agent produces a final answer from
    work already done, so the user gets a partial deliverable rather than a
    dead thread.
  - Run start is stamped with `setdefault` and deliberately **not** cleared by
    `after_agent`. The worker's goal-continuation loop invokes the graph more
    than once per run; clearing it would restart the clock each time and the
    deadline could never be reached. There is a test for exactly this.
  - `delegation_ledger` names `timeout_seconds` as the knob to raise for a
    `time_capped` run instead of `max_turns / token_budget`.

- Known limit (documented, not fixed here): the middleware only runs between
  model calls, so a run wedged inside one very long tool call sails past the
  deadline until that call returns. Bounding that needs a check outside the
  graph, e.g. in `RunManager.update_run_progress`, which is the only place that
  sees live cumulative totals during a run.

- Delete-when: upstream adds a wall-clock run budget and a partial-result
  contract for timed-out subagents.
- Upstream status: clean PR candidate for both halves. The salvage in
  particular is a strict improvement with an in-repo precedent to cite.

## Patch #71

**Patch #71 - Dynamic subagent-type listing in the `task` tool description** (landed 2026-08-23 as 7a7a3a64 + d760d406 with "Patch #65" in the commit subjects; renumbered 2026-09-02 because #65 was assigned twice that day and #79 refers to the UI simplification as #65; #71 had never been assigned)

- Class: generic-upstreamable (completes the Codex-style dynamic
  agent_type_description pattern for the tool schema)
- Intent: the `task` tool docstring hardcoded only the built-in types
  (`general-purpose`, `bash`) and mentioned custom types only as "may be
  defined in config.yaml". The tool schema is the most proximate guidance at
  call time, so on deployments with `subagents.custom_agents` the lead called
  `general-purpose` even when a specialist matched the work better — measured
  on atlas-nicholas thread 446ee9ea (2026-08-23): with patch #64 active and
  three parallel audit dispatches firing correctly, ALL three still used
  `general-purpose` (which is `model: inherit`), so the entire "fleet" ran on
  the lead's local model instead of the configured per-role cloud models.
  `task_tool_with_dynamic_types(app_config)` now returns an assembly-time
  COPY of the task tool whose description lists the ACTUAL available types
  from the registry — built-ins with compact notes, customs with their
  sanitized first-line description and explicit model ("Runs on model
  glm-5.3." / "Uses your model." for inherit). Called from
  `get_available_tools` when subagent tools are bound: each SUBAGENT_TOOLS
  entry that IS the task tool is swapped for the dynamic copy (other entries
  pass through, preserving the SUBAGENT_TOOLS membership contract pinned by
  test_tool_deduplication). The shared singleton and its static docstring
  (pinned by the routing-policy contract tests) are never mutated — the
  first cut mutated `task_tool.description` in place and CI caught
  cross-test pollution. Custom descriptions are agent-editable, so they are
  reduced to a single whitespace-collapsed line with angle brackets
  neutralized and a 240-char cap (same injection class as the
  `<subagent_system>` render site). The delegation policy text, When-to-use
  / When-NOT-to-use / Costs sections, and Args guidance are preserved
  verbatim; the `subagent_type` arg guidance now says "Pick the specialist
  that matches the work". Delegation-posture framing (patch #64) is
  unchanged — this patch fixes TYPE routing, not the dispatch posture.
- Files: `backend/packages/harness/deerflow/tools/builtins/task_tool.py`,
  `backend/packages/harness/deerflow/tools/tools.py`,
  `backend/tests/test_subagent_routing_prompt.py`
- Tests: `test_dynamic_types_list_customs_with_models`,
  `test_dynamic_types_preserve_pinned_guidance_and_args`,
  `test_dynamic_types_without_registry_entries_returns_shared_tool` (plus
  the pre-existing
  `test_general_purpose_and_task_descriptions_match_routing_policy`, which
  pins the static docstring, and
  `test_tool_deduplication.py::test_subagent_async_only_tool_gets_sync_wrapper`,
  which pins the SUBAGENT_TOOLS membership contract)
- Delete-when: never (additive; upstreaming encouraged — natural completion
  of the dynamic agent_type_description pattern the system prompt already
  uses).

## Patch #64

**Patch #64 - Config-gated `subagents.delegation_posture` prompt framing
(conservative / parallel_first)**

- Class: fork-local (argus deployment shaping; upstream has no fleet concept)- Intent: the upstream routing guidance tells the lead "Subagents are
  optional. **Default to direct execution.**" in FOUR rendered surfaces
  (`<subagent_system>`, the `<critical_reminders>` delegation line, the
  `<thinking_style>` DELEGATION CHECK, and the `task` tool docstring). That
  framing is right when subagents run the same model as the lead, but on
  deployments with a specialist subagent fleet (per-role stronger cloud
  models — argus "Ultra" mode) it is actively wrong: the measured result was
  zero delegations on a 9m23s docs-audit run where the lead serially grepped
  and fetched what an architect+researcher pair would have done in parallel
  (atlas-nicholas threads c4948cb6 / 1e24d614, 2026-08-23). A SOUL.md team
  briefing could not outvote four prompt surfaces. This patch adds
  `subagents.delegation_posture: conservative | parallel_first` (default
  `conservative` = byte-identical upstream wording) and, when set to
  `parallel_first` with a per-response limit > 1, re-frames the three
  prompt-rendered surfaces to team dispatch: the section becomes "Subagent
  Team: Parallelize Independent Scopes" with a SCOPE SCAN replacing the
  DELEGATION CHECK, the critical reminder becomes "Parallel Team Dispatch",
  and the thinking-style line becomes a "TEAM SCAN". HARD LIMITS,
  parallel-dispatch hard vetoes, available-subagent rendering, and
  per-run totals are identical in both postures so prompt and enforcement
  never disagree. The `task` tool docstring stays conservative (static
  module-level docstring; a dynamic variant would need tool rebuild wiring —
  revisit only if parallel_first still under-delegates with the three
  prompt surfaces flipped).
- Files: `backend/packages/harness/deerflow/config/subagents_config.py`,
  `backend/packages/harness/deerflow/agents/lead_agent/prompt.py`,
  `backend/tests/test_subagent_routing_prompt.py`,
  `backend/tests/test_subagent_timeout_config.py`
- Tests: `test_parallel_first_posture_flips_default_to_team_dispatch`,
  `test_parallel_first_posture_keeps_limits_and_vetoes`,
  `test_parallel_first_posture_keeps_conservative_framing_at_limit_one`,
  `test_conservative_posture_is_the_untouched_upstream_text`,
  `test_parallel_first_reminder_and_thinking_flip`,
  `test_delegation_posture_defaults_to_conservative`,
  `test_delegation_posture_accepts_valid_values`,
  `test_delegation_posture_rejects_unknown_values` (plus the pre-existing
  routing contract tests, which pin the conservative default verbatim)
- Delete-when: never (config knob; the conservative default IS upstream
  behavior). Drop only if upstream grows its own posture/mode-aware
  delegation framing worth switching to.

## Patch #65

**Patch #65 - Simplified shared workspace UI and serialized Telegram stage cleanup**

- Class: argus-edit (deployment-specific navigation/UI plus a generic channel
  race fix)
- Intent: keep Atlas and Pythia's shared chat surface focused on conversation.
  The sidebar exposes Chats plus direct new-tab links to Chronos, the
  Akropolis Handbook, and Feature Requests; Agents and Scheduled Tasks remain
  reachable by direct route and their APIs are unchanged. The chat header
  keeps the compact context-window gauge and debug sandbox action, removes the
  token dropdown, Browser trigger, and per-thread schedule shortcut, makes
  Export icon-only, and uses the brain marker for a running document title.
  Per-message and subtask token totals are no longer rendered, while all
  telemetry collection/folding stays intact. The context badge stays hidden
  until a real percentage is available instead of showing an inert icon-only
  placeholder beside the other header actions. Separately, Telegram stage emoji
  operations for one chat now share an async lock covering send, tracked-state
  replacement, prior-message deletion, and final cleanup. MessageBus dispatch
  stays fire-and-forget, but simultaneous thinking/tool/writing tasks can no
  longer observe the same predecessor, delete it more than once, and orphan
  their own replacement.
- Files: `frontend/src/components/workspace/` chat/sidebar/export/title,
  `frontend/src/app/workspace/agents/[agent_name]/chats/[thread_id]/page.tsx`,
  `backend/app/channels/_telegram_sender.py`, plus frontend/channel docs.
- Tests: `frontend/tests/e2e/sidebar.spec.ts`,
  `frontend/tests/e2e/workspace-simplification.spec.ts`, direct Agents and
  Scheduled Tasks route coverage, `thread-title.test.ts`, and
  `backend/tests/test_telegram_send.py::test_concurrent_stage_changes_are_serialized_and_final_cleans_latest`.
- Delete-when: split the UI half into deployment-owned frontend composition
  if DeerFlow gains supported nav/header slots. Drop the lock only if upstream
  replaces stage emojis with an atomic per-chat progress primitive or tracks
  and joins fire-and-forget stage tasks before final delivery.
- Upstream status: Telegram serialization is a generic bug-fix candidate; the
  navigation/header choices are Argus-specific.

## Patch #61

**Patch #61 - WebUI and server default `max_recursion_limit` bump to 10000**

- Class: generic-upstreamable
- Intent: LangGraph counts every middleware Pregel hop as a super-step (~17 hops
  per model turn with lead middlewares attached). A normal coding turn with ~59
  LLM calls hits the 1000 super-step ceiling and terminates with `GraphRecursionError`.
  Bumps WebUI client `recursion_limit` from 1000 to 10000 (submit and regenerate)
  and raises the Gateway clamp ceiling `max_recursion_limit` default from 1000 to 10000,
  providing ~580 LLM turns of headroom while loop detection and token budgets continue
  to catch real runaway loops.
- Files: `frontend/src/core/threads/hooks.ts` (EDITED: 1000 -> 10000 x2),
  `config.example.yaml` (EDITED: 1000 -> 10000),
  `backend/packages/harness/deerflow/config/app_config.py` (EDITED: default 1000 -> 10000),
  `backend/app/gateway/services.py` (EDITED: _DEFAULT_MAX_RECURSION_LIMIT 1000 -> 10000),
  `backend/tests/test_gateway_services.py` (EDITED: test assertions updated).
- Tests: `backend/tests/test_gateway_services.py` (run-config clamping tests updated).
- Delete-when: upstream raises `max_recursion_limit` / frontend `recursion_limit` to 10000 or counts turns instead of graph nodes.
- Upstream status: clean generic PR candidate.

## Patch #62

**Patch #62 - Telegram voice notes transcribed via overlay Deepgram STT**

- Class: argus-additive
- Intent: Telegram `message.voice` updates matched no handler, so the webhook
  200'd and the note was dropped. Register `filters.VOICE`, download via the
  existing `get_file` path, and soft-import `argus_telegram_stt.transcribe_voice`
  (Argus overlay, Deepgram nova-3). Success publishes inbound **text only**
  (`[Voice note transcript]\n…`); missing overlay, download failure, or STT
  error replies in chat and does **not** start an agent turn. Not an overlay
  `@tool` — tools never see Telegram updates.
- Files: `backend/app/channels/telegram.py` (EDITED: VOICE handler +
  `_publish_inbound_from_update` helper), `backend/app/channels/AGENTS.md`
- Tests: `backend/tests/test_channels.py` (`TestTelegramInboundMessages`
  voice cases)
- Delete-when: upstream Telegram adapter accepts voice notes and exposes an
  equivalent STT hook, or Argus stops using Telegram voice.
- Upstream status: not upstreamable as-is (Argus overlay import).

## Patch #63

**Patch #63 - Summarization must not resurrect answered user turns**

- Class: generic-upstreamable (already merged upstream — this is a backport)
- Intent: `_preserve_dynamic_context_reminders` rescued the dynamic-context
  ID-swap `__user` peer (the thread's FIRST user message) from every
  compaction with no staleness check, so an old, long-answered question kept
  masquerading as the live request while its answer was summarized away
  (atlas-nicholas thread `bacbf501`, 2026-08-22: the lead agent broke off
  mid-task and re-answered a 20-minutes-stale question twice). Now only
  tagged reminders plus the LATEST real user message (locked by exact id in
  `_prepare_compaction`) are rescued; stale `__user` peers compress like any
  other history.
- Files: `backend/packages/harness/deerflow/agents/middlewares/summarization_middleware.py`,
  `backend/tests/test_summarization_middleware.py`
- Tests: `test_stale_user_peer_is_compressed_not_rescued`,
  `test_stale_user_peer_compressed_without_memory`,
  `test_non_reminder_messages_with_double_underscore_id_not_rescued`,
  `test_current_request_survives_and_stale_peer_compresses` (upstream commit)
- Delete-when: the fork base includes upstream `0a3c04eb` (bytedance PR
  #4882, merged 2026-08-22) — drop this patch at the next base sync.
- Upstream status: ALREADY UPSTREAM — verbatim cherry-pick of `0a3c04eb`.

## Patch #20

**Patch #20 - view_image: vision-describe for non-vision lead models**

- Class: argus-edit
- Intent: When the lead model is non-vision (glm-planner on glm-nw), route
  each viewed image through the first `supports_vision` config model
  (local-qwen) for a render-verification-focused TEXT description and inject
  that instead of the raw image, so render-and-verify / view_image work on
  non-vision leads instead of being silently dead. Vision leads keep the
  direct image inject; describe is best-effort (failure injects a placeholder,
  never aborts the turn); the sync `before_model` defers to the async path.
- Files: `backend/packages/harness/deerflow/agents/middlewares/view_image_middleware.py` (EDITED),
  `backend/packages/harness/deerflow/agents/lead_agent/agent.py` (EDITED, attach for non-vision leads)
- Tests: `backend/tests/test_view_image_middleware.py` (EDITED, +cases)
- Delete-when: upstream adds vision-model routing for non-vision leads, or
  every Argus lead model is vision-capable (then the branch is unused; verify
  before deleting).
- Upstream status: none.

## Patch #21/#24

**Patch #21/#24 - surface the channel sender into ToolRuntime.context**

- Class: argus-edit (carried inside commit 1be4c909, shared with #30 because
  both edit the same `_CONTEXT_CONFIGURABLE_KEYS` frozenset)
- Intent: #21 puts the channel sender identity into the manager's run_context
  (`_resolve_run_params` -> `run_context_identity`); #24 whitelists those keys
  through `_CONTEXT_CONFIGURABLE_KEYS` in `gateway/services.py` so they
  actually reach `ToolRuntime.context` instead of being dropped at the
  gateway. This is how a tool (Pythia `correct_minutes`) attributes an action
  to the requesting human. Partially subsumed already: `channel_user_id`
  became upstream-native in v2.0.0 and was dropped from the patch; the carried
  keys are `channel_name`/`channel_id`/`thread_ts`.
- Files: `backend/app/channels/manager.py` (EDITED),
  `backend/app/gateway/services.py` (EDITED, whitelist)
- Tests: covered inside `backend/tests/test_channels.py` (EDITED) and the #30
  suites (`test_playbook_fire.py`) which exercise the same whitelist.
- Delete-when: upstream surfaces the remaining sender keys into the run
  context natively (it already did `channel_user_id`; watch each sync for the
  rest), or upstream makes runtime-context key whitelisting configurable.
- Upstream status: subsumed-watch (one of four keys absorbed in v2.0.0).

## Patch #22

**Patch #22 - Slack: thread-context for replies under non-agent posts**

- Class: argus-edit
- Intent: Pythia posts the minutes draft via a raw `chat.postMessage`, so that
  Slack thread has no DeerFlow thread; a reply like "assign Nicholas to
  speaker 2" otherwise starts a fresh agent thread with zero context.
  `SlackChannel.fetch_thread_context` pulls the thread's earlier messages via
  `conversations.replies` (excludes the current reply + bot acks, keeps bot
  posts since the draft IS the context); the manager new-thread branch
  prepends it to `msg.text`. Best-effort throughout (empty on any error,
  incl. missing_scope).
- Files: `backend/app/channels/slack.py` (EDITED),
  `backend/app/channels/manager.py` (EDITED)
- Tests: `backend/tests/test_channels.py` (EDITED, 5 new cases)
- Delete-when: the minutes draft is posted through the agent (creating a real
  DeerFlow thread), or upstream adds channel thread-history hydration.
- Upstream status: none.

## Patch #23

**Patch #23 - Slack: clean up the progress ack on completion**

- Carry-repair 2026-08-20: the 2026-08-15 rebase dropped the code half of this
  patch from slack.py while its carried tests survived; re-ported onto the
  current per-message web_client send path.

- Class: generic-upstreamable
- Intent: Upstream leaves the ":hourglass: Working on it..." reply and the
  :eyes: reaction in the thread forever, cluttering every turn. Record both in
  `self._acks` when the running reply is sent; delete the ack message and
  remove the reaction once the real answer posts (inside `post_message`, via
  the same per-message client). Best-effort; the ack entry is consumed so a
  later turn never double-clears.
- Files: `backend/app/channels/slack.py` (EDITED)
- Tests: `backend/tests/test_channels.py` (EDITED, +cases)
- Delete-when: upstream replaces or removes its own progress ack on
  completion.
- Upstream status: none (PR candidate per FORK-REVIEW).

## Patch coalesce

**Patch (unnumbered) - channels: coalesce split-paste messages**

- Class: argus-additive (strong upstream-PR candidate: the lost-message race
  hits any Telegram deployment)
- Intent: Telegram chunks a long paste into several InboundMessages within
  ~1s; the dispatch loop spawned a task per message, same-thread turns raced,
  and the 2nd+ hit 409 "thread busy" (runs use `multitask_strategy="reject"`;
  the runtime doesn't implement `enqueue`) and were silently LOST. New
  `MessageCoalescer` debounces CHAT messages per conversation
  (`DEFAULT_COALESCE_WINDOW=0.8s`) and dispatches the burst as ONE combined
  turn; commands bypass it; `coalesce_window<=0` restores the legacy immediate
  path; `stop()` flushes buffered bursts. Window configurable via
  `channels.coalesce_window`. (Pre-v2 this lived as item 4 of the old #10
  mega-section; it is its own logical patch now.)
- Files: `backend/app/channels/_coalesce.py` (NEW),
  `backend/app/channels/manager.py` (EDITED),
  `backend/app/channels/service.py` (EDITED, config forward)
- Tests: `backend/tests/test_message_coalesce.py` (NEW),
  `backend/tests/test_channels.py` (EDITED; 3 pre-coalescing thread-reuse
  tests pinned to `coalesce_window=0`)
- Delete-when: upstream ships message coalescing/debounce on the channel bus,
  or the runtime implements `multitask_strategy="enqueue"` so a burst queues
  instead of being rejected (then re-evaluate whether combining is still
  wanted for answer quality).
- Upstream status: none (PR candidate).

## Patch #30

**Patch #30 - scheduled-playbook fire endpoint + per-job agent & memory policy**

- Carry-repair 2026-08-20: the memory-injection half of the policy
  (`memory: off` suppresses injection) was disconnected when
  DynamicContextMiddleware grew the split reminder/memory message shape;
  re-wired via `effective_memory_mode(runtime)` at the first-turn injection
  site. Write-path gating had survived in write_policy.py.

- Class: argus-additive (new router + policy module; small edits at named
  seams)
- Intent: `POST /api/playbooks/<schedule_id>/fire`: a reconciled Chronos
  `channel_notify` job fires here; the endpoint reads
  `config/atlas-playbooks/<id>.md`, expands dates, discovers the citizen chat
  from the channel store, and publishes a synthetic InboundMessage (riding the
  #14 pipeline). The message carries a per-job agent (`agent_name`, resolved
  AHEAD of the channel's pinned agent in `_resolve_run_params`) and a per-job
  memory policy (`unattended=True` + `memory_mode` off|read-only|read-write,
  default read-only for unattended turns) enforced in BOTH memory-write paths
  (`MemoryMiddleware.after_agent` AND the summarization `memory_flush_hook`)
  plus injection suppression for `memory: off` in DynamicContextMiddleware.
  Internal-token guarded (the global token Chronos already holds); CSRF
  exempts valid-internal-token POSTs. Replaces the retired host-side
  `atlas-briefing.py` + systemd timers. Commit 3cc5491e is bookkeeping for
  this patch: two blocking_io test mocks updated to the changed
  DynamicContextMiddleware signatures.
- Files: `backend/app/gateway/routers/playbooks.py` (NEW),
  `backend/packages/harness/deerflow/agents/memory/write_policy.py` (NEW),
  `backend/app/gateway/app.py` (EDITED, router registration),
  `backend/app/gateway/csrf_middleware.py` (EDITED, internal-token exemption),
  `backend/app/gateway/services.py` (EDITED, whitelist keys, shared with #21/#24),
  `backend/app/channels/message_bus.py` (EDITED, InboundMessage fields),
  `backend/app/channels/manager.py` (EDITED, `_resolve_run_params`),
  `backend/packages/harness/deerflow/agents/middlewares/memory_middleware.py` (EDITED),
  `backend/packages/harness/deerflow/agents/memory/summarization_hook.py` (EDITED),
  `backend/packages/harness/deerflow/agents/middlewares/dynamic_context_middleware.py` (EDITED)
- Tests: `backend/tests/test_playbook_fire.py` (NEW),
  `backend/tests/test_per_job_memory.py` (NEW),
  `backend/tests/test_csrf_middleware.py` (EDITED),
  `backend/tests/test_channels.py` (EDITED),
  `backend/tests/blocking_io/test_dynamic_context_middleware.py` (EDITED, 3cc5491e)
- Delete-when: never - core product behavior (Chronos scheduled playbooks are
  the citizens' scheduling surface). Re-express if upstream grows first-class
  scheduled-turn / per-message-agent / per-turn-memory-policy mechanisms;
  surfaces to watch: `manager._resolve_run_params`, the
  `_CONTEXT_CONFIGURABLE_KEYS` whitelist, and the two memory-write paths.
- Upstream status: none.

## Patch #31/#32

**Patch #31/#32 - stay silent on contentless unattended turns**

- Class: argus-edit
- Intent: A scheduled (unattended) playbook fire with nothing to report must
  not post "(No response from agent)" (#31) or a lone filler token like "."
  that a model emits when told to produce no output (#32) to the citizen's
  chat every cron tick. `_is_trivial_unattended_text` (empty/whitespace, or
  <=3 chars with no alphanumeric) collapses such a response to empty and both
  delivery guards (non-streaming + streaming finally) suppress delivery on the
  unattended path only; the streaming path also suppresses intermediate stage
  emojis. Interactive turns and real errors are untouched (the streaming guard
  uses a `suppress_final` flag, not a bare return inside `finally`).
- Files: `backend/app/channels/manager.py` (EDITED, both delivery guards)
- Tests: `backend/tests/test_unattended_silence.py` (NEW, 19 cases; imports
  the predicate in isolation)
- Delete-when: never while unattended playbook turns post to chat - this is
  the wire-level backstop for the "produce no output when nothing to do"
  playbook convention. Re-express if upstream refactors the manager delivery
  guards or grows a suppress-empty-scheduled-output notion. Surface:
  `manager.py` final-dispatch guards.
- Upstream status: none.

## Patch #33

**Patch #33 - wire `sandbox.network` so DNS mode skips host-port publish**

- Class: generic-upstreamable (real fix for rootless-Podman deployments)
- Intent: The `sandbox.network` config key (from legacy #26) was never
  consumed: `LocalContainerBackend` always host-published `-p`, per-thread
  sandboxes collided on 8080/8081/8082 under rootless Podman, stuck in
  Created, and wedged runs holding the thread lock. Wire it end to end: when
  `network` is set, `--network <net>` and NO `-p`; sandbox URL becomes
  `http://<container_name>:8080` via container DNS; create/discover/
  list_running/destroy are all network-aware (no port alloc/release, orphan
  reconciliation adopts network-mode containers). Legacy host-publish path is
  byte-for-byte preserved when unset.
- Files: `backend/packages/harness/deerflow/community/aio_sandbox/local_backend.py` (EDITED),
  `.../aio_sandbox/aio_sandbox_provider.py` (EDITED),
  `backend/packages/harness/deerflow/config/sandbox_config.py` (EDITED, `network` field)
- Tests: `backend/tests/test_aio_sandbox_local_backend.py`,
  `backend/tests/test_aio_sandbox_provider.py`,
  `backend/tests/test_sandbox_orphan_reconciliation.py` (all EDITED, 11 new
  cases)
- Delete-when: upstream accepts network-mode for `LocalContainerBackend` (PR
  candidate: default-off, legacy path untouched), or upstream's sandbox
  backend abstraction gains a first-class no-publish/DNS mode.
- Upstream status: none (PR candidate).

## Patch #34

**Patch #34 - suppress narrated-silence announcements on unattended turns**

- Class: argus-edit
- Intent: #31/#32 handled empty and filler responses; the next escalation is a
  model narrating the decision ("No meetings in the window. Staying silent.")
  which the hourly meeting-prep poll posted to Telegram every tick.
  `_is_trivial_unattended_text` gains a third clause: a SHORT (<=120 char),
  phrasing-anchored nothing-to-report/staying-silent announcement is treated
  as empty. Deliberately tight (length-capped + pattern-anchored) so a genuine
  brief that mentions a meeting or the word "silent", or a longer digest
  opening "No meetings, but ...", survives. Unattended path only. The filter
  is the backstop, not the prompt.
- Files: `backend/app/channels/manager.py` (EDITED)
- Tests: `backend/tests/test_unattended_silence.py` (EDITED: +12 announcement
  cases that must blank, +4 real-brief cases that must survive)
- Delete-when: same family as #31/#32 (falls with them); additionally
  droppable if the lead model reliably stops narrating silence (model bump,
  verified over a canary window of scheduled polls).
- Upstream status: none.

## Patch #35

**Patch #35 - cut landing-galaxy CPU/GPU cost**

- Class: generic-upstreamable (pure perf fix to upstream's own landing hero)
- Intent: The WebGL galaxy (OGL fragment shader) ran a never-pausing full-rate
  rAF loop at full DPR with a 4-layer per-pixel star shader, heavy on
  weak/software-GL machines and even in backgrounded tabs. Adaptive loop:
  pause on hidden tab, honor `prefers-reduced-motion` (single static frame),
  cap ~30fps, cap backing-store DPR at 1.5, skip mouse lerp + listeners when
  `mouseRepulsion` is off. Lighter visuals: `NUM_LAYER` 4 -> 3, hero density
  0.6 -> 0.45, glowIntensity 0.35 -> 0.25.
- Files: `frontend/src/components/ui/galaxy.jsx` (EDITED),
  `frontend/src/components/landing/hero.tsx` (EDITED)
- Tests: none (no unit surface; validated via full `pnpm build`, the
  frontend/Dockerfile prod target)
- Delete-when: upstream fixes galaxy.jsx performance or replaces the landing
  hero.
- Upstream status: none (PR candidate).

## Patch #36

**Patch #36 - surface `(No response from agent)` on a blank final turn**

- Class: argus-edit
- Intent: local-qwen can end an interactive turn with empty final content
  while the last streamed partial is whitespace-only ("\n\n"); that whitespace
  is truthy, both delivery guards missed it, `to_telegram_html("")` produced
  zero chunks, and the reply silently vanished (bot went "working" -> nothing;
  observed 5 runs/2 days on atlas-nicholas). New `_is_blank_text()` (empty /
  whitespace-only / <=3-char non-alphanumeric filler) used on BOTH delivery
  paths so a blank final surfaces the visible marker instead of disappearing.
  It is the interactive-safe SUBSET of `_is_trivial_unattended_text` (omits
  the #34 announcement clause: "Nothing to report" is a real answer when
  attended); the unattended predicate composes it, #31/#32/#34 behavior
  unchanged. Visibility fix only; #37 is the root-cause retry.
- Files: `backend/app/channels/manager.py` (EDITED)
- Tests: `backend/tests/test_unattended_silence.py` (EDITED: +TestIsBlankText,
  +TestUnattendedStillCollapsesAnnouncements)
- Delete-when: upstream delivery guards handle blank/filler final text on
  channels, or the blank-final failure mode is conclusively gone (model bump +
  #37 metrics) AND we accept losing the visible marker as defense-in-depth.
- Upstream status: none.

## Patch #37

**Patch #37 - retry a blank final model turn + web display guard**

- Class: argus-edit (new middleware travels free; the edits are the pagination
  guard and wiring)
- Intent: #36 made blank finals visible; this fixes them. (1)
  `EmptyFinalRetryMiddleware` re-invokes the model ONCE when a FINAL turn is
  blank (AIMessage, no tool_calls, blank/filler content); a blank turn WITH
  tool_calls is a normal intermediate step and is left alone; bounded to one
  retry, registered before LoopDetection. (2) Web display guard
  `mark_blank_final_ai_messages()` in gateway pagination substitutes the
  marker for a blank last-ai-message-per-run on the thread-messages reload
  path, so the web UI never renders an empty answer even when a blank slips
  through. `is_blank_text` moved to `deerflow.utils.messages` (harness layer)
  so app and harness share it without a boundary violation
  (test_harness_boundary.py enforced); `manager.py` re-exports it.
- Files: `backend/packages/harness/deerflow/agents/middlewares/empty_final_retry_middleware.py` (NEW),
  `backend/packages/harness/deerflow/utils/messages.py` (EDITED upstream file, `is_blank_text`),
  `backend/app/gateway/pagination.py` (EDITED),
  `backend/app/gateway/routers/thread_runs.py` (EDITED),
  `backend/app/channels/manager.py` (EDITED, re-export/compose),
  `backend/packages/harness/deerflow/agents/lead_agent/agent.py` (EDITED, registration),
  `backend/CLAUDE.md` (EDITED, note)
- Tests: `backend/tests/test_empty_final_retry.py` (NEW: is_blank_text shapes,
  web-guard scoping, retry-once/no-loop/no-retry-on-tool-calls, sync+async)
- Delete-when: the middleware: the lead model stops emitting blank finals
  (model bump validated over a canary window) or upstream ships an equivalent
  final-turn retry; its registration re-expresses via #3809 when shipped. The
  web guard: upstream pagination guards blank finals on the read path.
- Upstream status: none (wiring: subsumed-watch on #3809).

## Patch #38

**Patch #38 - per-thread debug-sandbox link (#38a backend, #38b frontend)**

- Class: argus-additive (new endpoint body, new component; small edits at
  upstream touchpoints)
- Intent: A "Debug" button in both chat headers opens THAT thread's AIO
  sandbox UI (terminal/code-server/VNC/jupyter) so an operator can inspect the
  very container that ran the thread's sandbox tools. #38a:
  `POST /api/threads/{id}/debug-sandbox` acquires (or re-acquires, files
  rehydrated, OS state not) the thread's sandbox and returns
  `{hash, url: "/debug-sandbox/<hash>/"}`; guarded by
  `@require_permission("threads","read",owner_check=True,require_existing=True)`
  (auth boundary inherited, no new network exposure); 409 for non-container
  providers, 502 on acquire failure. #38b: `DebugSandboxTrigger` awaits
  acquire THEN `window.open` (popup-blocker + proxy-502-race safe); hides in
  welcome mode and after a 409. NOTE: the matching nginx
  `/debug-sandbox/<hash>/` reverse-proxy location is an argus-infra overlay
  (`config/nginx/nginx.conf`), NOT in this fork; applying it needs a
  per-project nginx restart, not just reload.
- Files: `backend/app/gateway/routers/threads.py` (EDITED, +80),
  `frontend/src/components/workspace/debug-sandbox-trigger.tsx` (NEW),
  `frontend/src/core/threads/api.ts` (EDITED, `acquireDebugSandbox`),
  `frontend/src/app/workspace/agents/[agent_name]/chats/[thread_id]/page.tsx` (EDITED),
  `frontend/src/app/workspace/chats/[thread_id]/page.tsx` (EDITED),
  `frontend/src/core/i18n/locales/{en-US,zh-CN,types}.ts` (EDITED)
- Tests: `backend/tests/test_threads_debug_sandbox.py` (NEW, 5 cases),
  `frontend/tests/unit/core/threads/api.test.ts` (EDITED, +3)
- Delete-when: never - core Argus operator tooling (pairs with the
  argus-infra nginx overlay and the deterministic `sha256(thread_id)[:8]`
  container naming). Re-express if upstream refactors `routers/threads.py` or
  the chat-header component layout; could be offered upstream as a generic
  feature if the sandbox-provider id gating is generalized.
- Upstream status: none.

## Patch #39

**Patch #39 - bound the checkpointer pool (elastic 1..4, idle-shrink)**

- Class: generic-upstreamable
- Status note: merged to `argus` as `f37b8292` (PR #4 squash-merge of branch
  `argus-patch-39-pool-bounds`; the pre-merge commit was `348739fc`). This
  line was stale until patch #40's PR: the d8ef0d13 ledger rebuild was
  authored against 2df36c99, before the merge landed.
- Intent: psycopg_pool's default is a fixed pool (max_size falls back to
  min_size=4), so every uvicorn worker permanently held 4 idle Postgres
  connections; with 2 workers per gateway and one gateway per project stack,
  the idle floor alone exhausted the shared server's max_connections on
  2026-07-01 ("FATAL: sorry, too many clients already"). Keep the per-worker
  ceiling (4) but make the pool elastic: min_size=1, grow under load, shrink
  after max_idle=300s; env-overridable via `DEERFLOW_CHECKPOINTER_POOL_MIN` /
  `_MAX` / `_MAX_IDLE`. Keepalive kwargs and check_connection wiring
  unchanged.
- Files: `backend/packages/harness/deerflow/runtime/checkpointer/async_provider.py` (EDITED, +16 in `_build_postgres_pool`)
- Tests: `backend/tests/test_checkpointer_pool_bounds.py` (NEW: defaults, env
  overrides, keepalive/check wiring; psycopg faked via sys.modules)
- Delete-when: upstream `langgraph-checkpoint-postgres` accepts pool kwargs
  passthrough, or bytedance accepts a checkpointer pool config knob; then the
  bounds move to config and the code patch drops.
- Upstream status: none (fork PR #4; both upstream candidates named above).

## Patch #40

**Patch #40 - Telegram send-path extraction to `_telegram_sender.py` (merge-tax reduction)**

- Carry-repair 2026-08-20: the 2026-08-15 rebase applied this patch as a stale
  file snapshot, silently reverting upstream telegram evolution the graft had
  already absorbed (#4392 inbound attachments, #4387 rich messages, #4800
  bounded intake, #4816 threadsafe shutdown drain). telegram.py was rebuilt as
  upstream + the argus grafts with the three shims re-extracted on top; the
  superseded upstream send/stream/rich helpers stay verbatim-but-unreachable,
  and TestTelegramStreaming now pins those fenced copies explicitly (the live
  argus path is locked by tests/test_telegram_send.py).

- Class: argus-edit (refactor of #9-chain's telegram.py half; net NEGATIVE
  carry - telegram.py drops from 574 to 251 changed lines vs v2.0.0, and
  `app/channels/` app-code carry from 1099 to 776)
- Intent: telegram.py was the fork's dominant merge tax (FORK-REVIEW lever
  #1). Zero behavior change (the pre-existing suite, unmodified, is the
  proof): the argus send path - stage-emoji indicator (show/promote/clear),
  HTML chunked sends with retry + plain-text fallback - moves verbatim into
  argus-owned `_telegram_sender.py` as channel-first free functions; ALL
  mutable state stays on the channel instance (`init_state(channel, config)`
  called from `__init__`) because tests read/patch `ch._working_msg` & friends
  directly, and telegram.py keeps three bound-method shims (`send`,
  `_send_running_reply`, `_send_running_reply_safe`) because tests and the
  receive path override/dispatch per instance (`_send_running_reply_safe` goes
  through `self._send_running_reply`, never the module function). Upstream
  v2.0.0's superseded edit-in-place stream helpers (`_send_stream_update` ..
  `_split_message`, their module constants, `_monotonic`, and the
  `_stream_messages` init) are RESTORED byte-identical but unreachable -
  `send()` never routes to them - so upstream churn in those regions merges
  clean instead of modify/delete-conflicting on every sync. Numbering note:
  inline `[argus patch #10]` comments inside the moved bodies are historical
  labels for the #9-chain stage-emoji work; left verbatim on purpose.
- Files: `backend/app/channels/_telegram_sender.py` (NEW),
  `backend/app/channels/telegram.py` (EDITED - shrunk; residual argus content
  is the #17 welcome/lock, the #28 webhook mode, the three #40 shims +
  `init_state` call, and the `_log_future_error` static override noted below)
- Tests: `backend/tests/test_telegram_sender_seam.py` (NEW - delegation +
  contract locks so a naive future merge that resurrects upstream's send body
  fails loudly); behavior proof is the UNTOUCHED existing suite
  (`test_telegram_send.py`, `test_channels.py` Telegram classes,
  `test_telegram_channel_connections.py`).
- Delete-when: with #9-chain (this module hosts its telegram-side behavior).
  If #9-chain is upstreamed or re-expressed behind an upstream extension
  point, `_telegram_sender.py` goes with it and telegram.py returns to
  vanilla + #17/#28.
- Upstream status: n/a (fork-internal restructuring).
- Follow-up candidate (NOT in #40, would change log wording): telegram.py's
  static `_log_future_error` shadows the base-class method upstream v2.0.0
  already provides; deleting the override would shave ~9 more carried lines
  but is a real (if tiny) behavior delta - out of scope for a zero-behavior
  patch.

## Patch #41

**Patch #41 - coerce a stringified `write_todos` arg in the planner pipeline**

- Class: argus-additive (edits only the argus-owned
  `argus_todo_middleware.py`; zero upstream-file carry)
- Intent: glm-nw sometimes double-encodes the `write_todos` tool argument -
  `args.todos` arrives as the JSON STRING of a valid list instead of a native
  array. Pydantic rejects it (`todos: list[Todo]`), the agent gets an error
  ToolMessage, and `state.todos[]` never hydrates; caught by the weekly eval
  2026-07-02 (pythia/planning on glm-planner: "write_todos was called but its
  'todos' arg is str, expected list"). `ArgusTodoMiddleware.after_model` now
  parses a string arg in place (only when it json-parses to a list) BEFORE
  tool validation, so the trajectory records the normalized call and eval
  graders stay strict. Unparseable strings keep the normal validation-error
  path. The sibling flake (plan.json written but write_todos never called) is
  NOT patched - it is model behavior the weekly eval baseline tracks.
- Files: `backend/packages/harness/deerflow/agents/middlewares/argus_todo_middleware.py`
  (argus-owned file; also refreshed its stale "qwen-local-coder" selection
  note to the #18 `uses_planner_pipeline` gate)
- Tests: `backend/tests/test_argus_todo_middleware.py` (+3: coercion,
  unparseable-left-alone, native-list/other-tools untouched)
- Delete-when: glm-nw (or a replacement planner model) reliably emits native
  arrays, or upstream langchain's todo tool grows arg coercion.
- Upstream status: none (candidate: the coercion is generic enough for
  upstream langchain's TodoListMiddleware).

---

## Patch #42

**Patch #42 - subtask card false-"failed" on transient SSE loading gaps**

- Class: bugfix (frontend-only; edits upstream `subtask-result.ts`,
  `message-list.tsx`, and unit tests)
- Intent: `derivePendingSubtaskStatus` returned `"failed"` whenever
  `isCurrentTurnLoading` was false and no ToolMessage had arrived yet.
  During long-running background subagent tasks (900s timeout), the SSE
  stream can briefly show `isLoading=false` (connection pause, reconnection,
  checkpoint flush). The `"failed"` status sticks because
  `useUpdateSubtask`'s terminal-guard prevents `"in_progress"` from
  overwriting a terminal status. When the ToolMessage eventually arrives,
  `parseSubtaskResult` does set the correct status, but in some SSE
  delivery paths the tool result is delayed or grouped differently, so the
  false "failed" persists in the UI. The fix adds an `isLastGroup`
  parameter: when the subagent group is still the last group in the
  thread, the function returns `"in_progress"` instead of `"failed"`
  (the turn may still be in progress with a transient loading=false).
  Only when the run has moved past this group (not the last group) does
  it return `"failed"`, preserving the "stale task from a prior turn"
  detection.
- Files: `frontend/src/core/tasks/subtask-result.ts` (added
  `isLastGroup` param + JSDoc),
  `frontend/src/components/workspace/messages/message-list.tsx` (pass
  `isLastGroup` from the group index),
  `frontend/tests/unit/core/tasks/subtask-result.test.ts` (updated
  existing test to pass `false`, added test for `true` case)
- Tests: `frontend/tests/unit/core/tasks/subtask-result.test.ts` (+1:
  "stays in_progress for the last group even when loading is briefly false";
  updated: "does not revive an earlier unfinished task" now passes
  `isLastGroup=false`)
- Delete-when: upstream's `BaseStream.isLoading` reliably stays `true`
  during active background tool execution, or when the task_tool moves
  to a push-based result delivery that doesn't rely on polling + SSE.
- Upstream status: none (candidate: the race is inherent to the
  polling-based task_tool design; upstream may have a different fix
  approach).

---

## Patch #43

**Patch #43 - per-run allowed-tools from schedule frontmatter**

- Class: argus-additive (extends the #30 fire endpoint and tool policy; small
  edits at named seams)
- Intent: A self-contained schedule (all instructions in the prompt body) no
  longer needs a corresponding skill file purely to declare `allowed-tools`.
  The schedule's YAML frontmatter may now carry an `allowed-tools` list (same
  format as skills). At fire time, the reconciler passes it through Chronos
  job metadata; the gateway forwards it on `InboundMessage.allowed_tools`; the
  channel manager seeds it into `run_context["allowed_tools"]`; `_make_lead_agent`
  reads it from the runtime config and passes it as `extra_allowed` to
  `filter_tools_by_skill_allowed_tools`, which unions it with the skill-based
  whitelist. If no skill declares allowed-tools, the schedule's list becomes the
  sole whitelist; if skills do declare, the two sets are unioned. A schedule
  without `allowed-tools` is byte-for-byte unchanged (legacy behavior).
- Files: `backend/app/channels/message_bus.py` (EDITED, +field),
  `backend/app/gateway/routers/playbooks.py` (EDITED, +request field, +forward),
  `backend/app/channels/manager.py` (EDITED, +run_context),
  `backend/app/gateway/services.py` (EDITED, +whitelist key, shared with #21/#24/#30),
  `backend/packages/harness/deerflow/skills/tool_policy.py` (EDITED, +param),
  `backend/packages/harness/deerflow/agents/lead_agent/agent.py` (EDITED, +read + pass)
- Tests: `backend/tests/test_playbook_fire.py` (EDITED, +2 cases:
  allowed_tools flow-through, default-None)
- Delete-when: upstream grows a per-run tool-whitelist mechanism (e.g. a
  configurable tool filter on the run request), or the schedule system is
  re-expressed with a native scheduling API that carries tool constraints.
- Upstream status: none (argus-additive; the tool_policy param has a clean
  default and is upstreamable as a generic extension).

---


## Patch #44

**Patch #44 - Unattended silence: no blank-final retry, wider narration backstop, no token logging** (record back-filled 2026-09-02; landed 2026-07-20 as 8a256f7d, fork PR #8; re-applied 2026-08-15)

- Class: argus-edit (of #37 and #34) + a logging pin in the gateway bootstrap.
- Intent: three fixes from the 2026-07-18/19 hourly Telegram spam. EmptyFinalRetryMiddleware (#37) no longer retries a blank final on an unattended turn: the blank, or the `.` no-op sentinel, is the desired silent outcome and the retry re-sampled the model into narrating. The #34 narrated-silence backstop cap goes 120 to 280 chars, `calendar/schedule is clear` and `nothing ... attention` count as announcement phrasing, and a contrast/alert-marker veto (`but`, `however`, `urgent`, `moved`, `cancelled`) protects genuine content. httpx/httpcore are pinned to WARNING in the gateway bootstrap so the Telegram bot token no longer lands in the journal on every send.
- Files: `backend/app/channels/manager.py`, `backend/app/gateway/app.py`, `backend/packages/harness/deerflow/agents/middlewares/empty_final_retry_middleware.py`.
- Tests: `backend/tests/test_empty_final_retry.py`, `backend/tests/test_unattended_silence.py`.
- Delete-when: upstream grows an unattended (scheduled) turn notion that suppresses blank-final retries and narration, and logs httpx at WARNING by default.
- Upstream status: none sent; the httpx logging pin is a generic candidate.

## Patch #45

**Patch #45 - Delivery-report callback for scheduled playbook fires** (record back-filled 2026-09-02; landed 2026-07-20 as 27a20421, fork PR #9)

- Class: argus-additive (`_delivery_report.py`) + argus-edit (manager outcome seams).
- Intent: Chronos expects a delivered|silent|failed callback per run; the gateway never called back, so every channel_notify run sat `running` for 1800s and closed as unreported. `report_delivery()` POSTs `{status, channel, chat_id, message_text, delivered_at, error}` to the run's `report_url` with the internal token, one retry, never raises (a report failure degrades to pre-#45 behavior). `PlaybookFireRequest` and `InboundMessage` carry an optional `report_url`; `manager._report_unattended_outcome()` fires at the silent (both unattended-suppression branches), delivered and failed seams.
- Files: `backend/app/channels/_delivery_report.py` (new), `backend/app/channels/manager.py`, `backend/app/channels/message_bus.py`, `backend/app/gateway/routers/playbooks.py`.
- Tests: `backend/tests/test_delivery_report.py`, `backend/tests/test_playbook_fire.py` (report_url flow-through, default None).
- Delete-when: upstream's scheduled-tasks MVP (4fc08b4f) grows a completion callback; reconcile together with #30/#43.
- Upstream status: none sent.

## Patch #46

**Patch #46 - `tool_search.exclude`: deferral opt-out for hot MCP tools** (record back-filled 2026-08-21; landed 2026-07-23 as 9803a9e3 — the #44-#52/#54 records were never written, this one is restored because #60 documents itself against it)

- Class: config-expressed (default `exclude: []` = upstream behavior).
- Intent: MCP tools defer to name-only by default; for hot most-turns tools
  the `tool_search` promotion round-trip costs more latency than their
  schemas cost context (measured ~13s/conversation on local-qwen,
  2026-07-23). `exclude` (read: exclude FROM deferral; preferred alias since
  #60: `always_bind`) pins matching final tool names always-bound.
- Files: `backend/packages/harness/deerflow/config/tool_search_config.py`,
  `backend/packages/harness/deerflow/tools/builtins/tool_search.py`, the
  assemble call sites.
- Tests: `backend/tests/test_deferred_setup.py` (TestExclude).
- Delete-when: with #60's `always_bind` alias, when upstream ships an
  equivalent pin list.
- Upstream status: none sent; generic candidate together with #60.

## Patch #47

**Patch #47 - Per-lead-model summarization overrides** (record back-filled 2026-09-02; landed 2026-07-30 as 8e2b51b5 + ca0cfe85, fork PR #11)

- Class: config-expressed (absent `per_model` = byte-identical behavior).
- Intent: `summarization` was one global block, but both the trigger and the summarizer depend on the lead model's window (local-qwen 131k vs glm-nw 1M: a glm run summarized at 8.7% of its window and handed a 1M-token thread to a 131k summarizer). `SummarizationConfig` gains `per_model` + `resolved_for(lead_model_name)`; only fields set on an override apply; the resolved view drops `per_model` so a second resolution cannot compound. `fraction: 0.7` is not an option because our LiteLLM aliases carry no model profile.
- Files: `backend/packages/harness/deerflow/config/summarization_config.py`, `backend/packages/harness/deerflow/agents/lead_agent/agent.py` (`_create_summarization_middleware(lead_model_name)`).
- Tests: `backend/tests/test_summarization_per_model.py`; ca0cfe85 relaxed two brittle stubs in `backend/tests/test_lead_agent_model_resolution.py`.
- Delete-when: upstream resolves summarization thresholds from the lead model without requiring a model profile.
- Upstream status: none sent; generic candidate.

## Patch #48

**Patch #48 - Fail-closed Pythia retrieval ring** (record back-filled 2026-09-02; landed 2026-07-30 as 8eec0f98)

- Class: argus-edit (of #11).
- Intent: retrieval was gated fail-open: an agent that declared no `pythia_ring` inherited `internal` from `PYTHIA_ROUTER_INJECT`, so UI threads on stacks whose agent opted out (`pythia_ring: none`) received six blocks of unrelated company knowledge per turn and the model learned to dismiss its own context (0.5 to 1.0s extra latency per turn). Retrieval now attaches only when an agent declares a known ring; absent, empty, `none` and unrecognised all mean no retrieval, in both the build gate and the constructor. `PYTHIA_ROUTER_INJECT` (alias `PYTHIA_RETRIEVAL_ENABLED`) is a kill switch only.
- Files: `backend/packages/harness/deerflow/agents/lead_agent/agent.py`, `backend/packages/harness/deerflow/agents/middlewares/pythia_retrieval_middleware.py`, `backend/packages/harness/deerflow/config/agents_config.py`.
- Tests: `backend/tests/test_pythia_retrieval_middleware.py`.
- Delete-when: together with #11 (Pythia retrieval is Argus-specific).
- Upstream status: n/a (Argus-specific).

## Patch #49

**Patch #49 - Omitted-item index in list-shaped tool-output previews** (record back-filled 2026-09-02; landed 2026-08-04 as 7dd2ed96; re-integrated into the synopsis preview by db9f5701 on 2026-08-20)

- Class: argus-edit.
- Intent: a head+tail preview of a `---`-separated list (kb-api's listing tools) kept the first and last item and silently dropped the middle while reading as complete; the 2026-08-03 daily review reported two 1:1s' minutes as not captured because they sat in the omitted span of a 22.6K `pythia_list_meetings` result. The preview marker now indexes the dropped blocks (first line each, cap 24 x 160 chars, `+N more`). Non-list previews are byte-identical; `_build_fallback` keeps its hard `max_chars` contract.
- Files: `backend/packages/harness/deerflow/agents/middlewares/tool_output_budget_middleware.py`.
- Tests: `backend/tests/test_tool_output_budget_middleware.py` (TestBuildPreviewOmittedBlockIndex).
- Delete-when: upstream's preview builder indexes omitted spans.
- Upstream status: none sent; generic candidate.

## Patch #50

**Patch #50 - Connector call proxy and app overlay-tools proxy** (record back-filled 2026-09-02; landed 2026-08-11 as 8364d025, committing image drift that had shipped since 2026-08-06)

- Class: argus-additive (two routers) + argus-edit (auth and CSRF middleware exemptions).
- Intent: `/api/connectors/*` (legacy `/api/transformers/*` alias) proxy for app frontends with CORS for `apps-*` origins and credentials off; `/api/apps/{slug}/tools/{name}` overlay-tool proxy with two-layer scoping (infra `http-exposed-tools.json` plus per-app `app.json` `http_tools`); the auth middleware passes OPTIONS preflights and CSRF exempts both prefixes. Code comments in `tools_proxy.py` call themselves #51 because the merged PR #15 had already taken that number; the tools proxy is #50b in this ledger.
- Files: `backend/app/gateway/app.py`, `backend/app/gateway/auth_middleware.py`, `backend/app/gateway/csrf_middleware.py`, `backend/app/gateway/routers/tools_proxy.py` (new), `backend/app/gateway/routers/transformers_proxy.py` (new).
- Tests: none in the commit (landed verbatim from the running image); exercised by the Argus-side edge and transformer suites.
- Delete-when: together with the Argus app tier.
- Upstream status: n/a (Argus-specific).

## Patch #51

**Patch #51 - Inline connector prompts on the playbook fire endpoint** (record back-filled 2026-09-02; landed 2026-08-10 as 4bae86f6 (plus ruff commits 0d3d4d3a, b3b3130f), fork PR #15)

- Class: argus-edit (of #30).
- Intent: connector-pinned prompts live in Chronos's registry, not in `config/atlas-playbooks/`, so `PlaybookFireRequest` gains optional `prompt_text`; when set the file lookup is skipped and the path id is attribution only. 422 on whitespace-only or >64KB; date placeholders expand on both paths. Same internal-token trust boundary: the sender (Chronos) pins the prompt and gates the data frame.
- Files: `backend/app/gateway/routers/playbooks.py`.
- Tests: `backend/tests/test_playbook_fire.py` (TestFirePromptText).
- Delete-when: together with #30.
- Upstream status: n/a (Argus-specific).

## Patch #52

**Patch #52 - Scheduled fires deliver to root chats only** (record back-filled 2026-09-02; landed 2026-08-10 as c10080d6, fork PR #16)

- Class: argus-edit (of #30).
- Intent: `fire_playbook` iterated every store entry including per-topic thread rows, so one `hook:<connector>` thread (#51) made every scheduled fire deliver twice into the same chat: a coalesced doubled prompt, silence suppression lost, hourly `(No response from agent)` to the citizen. Filter to root rows (no `topic_id`); an only-topic store is 409 like no mapping.
- Files: `backend/app/gateway/routers/playbooks.py`.
- Tests: `backend/tests/test_playbook_fire.py` (TestFireTargetsRootChatsOnly).
- Delete-when: together with #30.
- Upstream status: n/a (Argus-specific).
- Note: the coalesce path appears to drop the unattended flag (an empty-final retry ran on an unattended merged turn despite #44); unreachable for scheduled fires after this patch, not fixed.

## Patch #53

**Patch #53 - Agent-level tool policy (`tool_policy.source: agent`)** (record back-filled 2026-09-02; landed 2026-08-11 as 457a984e, fork PR #18; runtime honouring followed in #58)

- Class: config-expressed (default `source: skills` = byte-identical upstream behavior).
- Intent: config-gated alternative to upstream PR #2626's skill-union enforcement. Under `source: agent`, `AgentConfig.allowed_tools` is the run's whitelist (omitted = no restriction, `[]` = no tools, list = exactly those); the firing schedule's allowed-tools (#43) still union in and become the sole whitelist on an unrestricted agent, so unattended runs stay scopable; skill allowed-tools demote to documentation and tool_search promotion hints; subagents inherit the parent ceiling via run metadata (`agent_allowed_tools`).
- Files: `backend/packages/harness/deerflow/agents/lead_agent/agent.py`, `backend/packages/harness/deerflow/config/agents_config.py`, `backend/packages/harness/deerflow/config/app_config.py`, `backend/packages/harness/deerflow/config/tool_policy_config.py`, `backend/packages/harness/deerflow/skills/tool_policy.py`, `backend/packages/harness/deerflow/subagents/executor.py`.
- Tests: `backend/tests/test_tool_policy_agent_source.py`, TestAgentSourceToolPolicy in `backend/tests/test_subagent_executor.py`.
- Delete-when: upstream ships an agent-level allowed-tools ceiling (watch the PR #2626 line).
- Upstream status: none sent; generic candidate.

## Patch #54

**Patch #54 - Preserve Qwen `reasoning_content` across turns (PatchedChatQwen)** (record back-filled 2026-09-02; landed 2026-08-12 as 77567a8f)

- Class: argus-additive (new model class; opt-in via `use: deerflow.models.patched_qwen:PatchedChatQwen`).
- Intent: stock `langchain_openai` drops `reasoning_content` both ways (never extracted from responses, never re-injected on outbound serialisation), so Qwen's `preserve_thinking` template flag was a no-op for DeerFlow. PatchedChatQwen extracts `reasoning_content` on the stream (per delta) and non-stream paths into `additional_kwargs`, and re-injects it on outbound assistant messages only when the request carries `chat_template_kwargs.preserve_thinking: true`; otherwise the payload is byte-identical to stock.
- Files: `backend/packages/harness/deerflow/models/patched_qwen.py` (new).
- Tests: `backend/tests/test_patched_qwen.py` (red-checked against stock ChatOpenAI).
- Delete-when: `langchain_openai` round-trips `reasoning_content` natively.
- Upstream status: none sent; the langchain side is the real target.

## Patch #67

**Patch #67 - Rejoin an in-flight run on WebUI reload + disconnect-safe viewer joins**

- Class: generic-upstreamable (frontend-new-file + edits to upstream
  `hooks.ts`/`message-list.tsx`/`subtask-result.ts`/`api-client.ts` +
  `0`-default backend param on existing join endpoints).
- Intent: #59 made WebUI runs survive navigation (`onDisconnect: continue`),
  but the only reload-rejoin path was the SDK's `reconnectOnMount`, which reads
  a `sessionStorage` key written only by the submitting tab. A new tab, closed
  browser, or shared link has no key, so reopening a thread whose run is still
  active server-side renders stale history: `isLoading` stays false, the last
  turn looks settled ("Completed in …"), and dangling `task` tool calls surface
  as "Subtask failed" — even though the subagents are still running.
  This adds `useActiveRunRejoin`: on mount, when no stream is live and the SDK
  same-tab key is absent, it lists the thread's runs, rejoins the newest
  active one via the SDK `joinStream` (writing the reconnect key so Stop/cancel
  keep working), and exposes its id. Existing machinery owns every race:
  terminal-before-join (api-client preflight skips), evicted buffer
  (`stream_replay_gap` recovery), other-worker 409 (inactive).
  Backend safety: channel/scheduled runs are created with `on_disconnect=cancel`
  (SDK default when `stream_resumable` is unset), so a viewer that joined such a
  run and navigated away would have cancelled it. The join/stream endpoints now
  honor a tristate `cancel_on_disconnect` query param (None = run's own policy,
  false = never, true = cancel); the SDK always sends `cancel_on_disconnect=0`
  on `joinStream`, so viewer rejoins never abort work they did not start.
  Frontend fallback: `derivePendingSubtaskStatus` gains `owningRunIsActive`;
  a dangling call whose owning run is the rejoined active run renders
  `in_progress` instead of `failed`, covering the pre-`isLoading`-flip window
  and join-failure degradation.
- Files: `frontend/src/core/threads/active-run-rejoin.ts` (NEW),
  `frontend/src/core/threads/hooks.ts` (+hook call, +`activeRunId` return),
  `frontend/src/core/api/api-client.ts` (export `rememberReconnectRun`),
  `frontend/src/components/workspace/messages/message-list.tsx` (+prop,
  pass `owningRunIsActive`), `frontend/src/components/workspace/{chats/chat-page,
  sidecar/sidecar-panel}.tsx` +
  `frontend/src/app/workspace/agents/[agent_name]/chats/[thread_id]/page.tsx`
  (thread `activeRunId`), `frontend/src/core/tasks/subtask-result.ts`
  (+param), `backend/app/gateway/services.py` (`_should_cancel_on_disconnect`
  + `sse_consumer` override), `backend/app/gateway/routers/thread_runs.py`
  (`join_run`/`stream_existing_run` +tristate param).
- Tests: `backend/tests/test_gateway_services.py` (resolver + 4-case
  parametrized override), `frontend/tests/unit/core/threads/active-run-rejoin.test.ts`
  (`pickActiveRun`), `...active-run-rejoin.dom.test.tsx` (5 rejoin-hook cases),
  `frontend/tests/unit/core/tasks/subtask-result.test.ts` (+4 fallback cases).
- Delete-when: upstream `@langchain/langgraph-sdk` `useStream` reconnects to an
  active run discovered at runtime (not just a same-tab sessionStorage key),
  and the join endpoints honor `cancel_on_disconnect` upstream.
- Upstream status: none sent. Supersedes the unmerged #42 `isLastGroup`
  approach by fixing the root cause (rejoin) plus a scoped fallback; #42's
  commit is not in argus history.

## Patch #68

**Patch #68 - Loop-detection: result-aware hard-stop gating + `no_hard_stop_tools`**

- Class: argus-edit (behavioral edits to upstream middleware + config; the
  `no_hard_stop_tools`/`recoverable_retry_limit` halves are config-expressed,
  and the whole patch is a generic-upstreamable candidate).
- Intent: Layer 1 hard-stops on identical `(tool, args)` hashes without
  looking at WHY the model is retrying. When the tool's own stamped
  `deerflow_tool_meta` says the failure is model-recoverable (`no_results`,
  `not_found`, `permission`), ToolProgressMiddleware deliberately keeps the
  tool WARNED-not-BLOCKED ("the model can fix this by changing strategy") —
  but loop detection killed the run on the identical retry anyway. Observed:
  atlas-nicholas 2026-08 (thread b8a3dcd1), 8 identical `code_search_logs`
  calls each answered "No log entries for '...'" ended the whole run with an
  empty forced final answer, destroying accumulated investigation work. As
  the stacks operate more and more as coding harnesses, this class of
  false-positive kill became the top source of frustration.
  Changes:
  1. **Result-aware gate**: a Layer-1 hard stop downgrades to an escalating
     warning when EVERY repeated tool's most recent result meta is a
     model-recoverable soft failure. The stop still fires unchanged for
     `success` results (classic identical re-read runaway), unrecoverable
     errors (auth/config/internal/rate_limited), metas stamped
     `source=progress_middleware` (tool BLOCKED — hammering it is a real
     loop), and missing meta (conservative).
  2. **`recoverable_retry_limit`** (default 24, ~3x a typical hard_limit):
     downgrades are bounded — past this many identical calls the detector
     stops coddling and hard-stops anyway, bounding the quadratic context
     cost of a loop that ignores escalating warnings.
  3. **`no_hard_stop_tools`** (default `[]`): absolute per-tool opt-out from
     hard stops on both layers (warn-only forever); cost backstops are
     token_budget + run_deadline. Applies only when the whole repeated call
     set is exempt.
  4. **Layer 2 stays meta-blind** (volume cap, not identical-repeat
     detection) — only `no_hard_stop_tools` exempts there;
     `tool_freq_overrides` already exist for legitimately chatty tools.
  5. **Actionable warnings**: warn/downgrade messages now name the tools and
     the repeat count, and the downgrade message carries the meta's
     `recommended_next_action` as concrete advice ("rewrite the query:
     change the search terms, filters, or scope") instead of a bare
     "wrap up".
  6. **Empty-result markers** in `tool_result_meta.py`: `_PARTIAL_MARKERS`
     and the `no_results` error rule grow the real fleet phrasings
     ("no log entries", "no code matches", "no matches found", "no matching",
     "no entries", "no events", "no rows", "nothing found", bare "no
     results") so those bodies stamp `partial_success/rewrite_query` —
     without this, "No log entries for ..." classified as plain `success`
     and neither tool_progress stagnation nor the new gate could see it.
- Files: `backend/packages/harness/deerflow/agents/middlewares/loop_detection_middleware.py` (EDITED),
  `backend/packages/harness/deerflow/config/loop_detection_config.py` (EDITED),
  `backend/packages/harness/deerflow/agents/middlewares/tool_result_meta.py` (EDITED),
  `config.example.yaml` (EDITED, loop_detection section docs)
- Tests: `backend/tests/test_loop_detection_middleware.py` (EDITED,
  +`TestResultAwareHardStopGating` with 15 cases),
  `backend/tests/test_loop_detection_config.py` (EDITED, +5),
  `backend/tests/test_tool_result_meta.py` (EDITED, +14 parametrized)
- Delete-when: upstream ships an equivalent result-aware hard-stop gate
  (consulting `deerflow_tool_meta` recoverability before stripping
  tool_calls), or upstream adopts `no_hard_stop_tools`. The marker list is
  independently upstreamable and may land separately.
- Upstream status: none sent (strong PR candidate; the gate rationale cites
  Gemini CLI's loop-recovery design, which soft-recovers before killing).

## Patch #69

**Patch #69 - Loop-detection: near-duplicate SUCCESS downgrades (content Jaccard)**

- Class: argus-edit (behavioral extension of patch #68's gate; generic-
  upstreamable candidate alongside it).
- Intent: patch #68's gate honored the stamped `deerflow_tool_meta` only —
  `status == "success"` always kept the hard stop ("classic re-read").
  But a success whose CONTENT is a near-duplicate of the tool's own recent
  successes carries the same "no new information" signal as a `no_results`
  soft failure, and ToolProgressMiddleware already treats it that way
  (Jaccard over recent word sets). Observed: thread 9dc15e99 (2026-08-27,
  after #68's fleet rollout), a paired `pythia_query` call re-issued the
  IDENTICAL pair ≥5 times within the window, each round returning the same
  successful chunks (scores varying marginally), and the run died at
  hard_limit 8 with another empty `[FORCED STOP]` — the user's exact same
  complaint class #68 was meant to fix. The gate now compares the stamped
  content: near-duplicate → downgrade to the escalating warning (bounded by
  `recoverable_retry_limit` like all other downgrades); success with FRESH
  content keeps the hard stop (true re-read/progress detection). The
  similarity helpers (`word_set`, `is_near_duplicate`) are reused from
  ToolProgressMiddleware, threshold 0.8 / min_words 10 (ToolProgressConfig
  defaults), judged per tool name over latest result + up to 3 priors.
  Content too short to judge, like missing meta, remains the conservative
  hard stop. `_outcome_phrase` gains a "near-duplicate results" branch so
  the downgraded warning names what the model is doing.
- Files: `backend/packages/harness/deerflow/agents/middlewares/loop_detection_middleware.py` (EDITED)
- Tests: `backend/tests/test_loop_detection_middleware.py` (EDITED, +5 cases
  in `TestResultAwareHardStopGating`: paired-near-duplicate downgrade,
  retry-limit bound, distinct-content keeps stop, short-content conservative
  stop, whole-set similarity rule)
- Delete-when: upstream ships content-similarity-aware gating (or accepts
  this PR along with #68's gate).
- Upstream status: none sent.

## Patch #70

**Patch #70 - Surface missing durable run-event storage** (record back-filled 2026-09-02; landed 2026-08-27 as 477d652a, fork PR #44)

- Class: generic-upstreamable.
- Intent: the a97e7b74 postmortem found every Argus schema missing `run_events` while stacks configured `run_events.backend=db`: `database.backend` defaulted to memory, `make_run_event_store` fell back to `MemoryRunEventStore` silently, and persistence bootstrap never ran. Now the factory logs ERROR when `run_events.backend=db` has no SQL session factory, and bootstrap reflects `has_run_events` and logs ERROR when a versioned PostgreSQL schema lacks the table, without auto-creating it (schema repair must be deliberate).
- Files: `backend/packages/harness/deerflow/persistence/bootstrap.py`, `backend/packages/harness/deerflow/runtime/events/store/__init__.py`.
- Tests: `backend/tests/test_persistence_bootstrap.py`, `backend/tests/test_run_event_store.py`.
- Delete-when: upstream fails loudly on a db run-event backend without a session factory.
- Upstream status: none sent; generic candidate.

## Patch #72

**Patch #72 - Atomic edit batching + soft execution-phase budgets**

- Class: config-expressed (generic behavior with disabled defaults; Argus
  enables the soft budgets in Atlas project configuration).
- Intent: Local models lose most wall time to a model round trip between each
  tiny read and edit. `str_replace.replacements` applies up to 50 ordered edits
  to one in-memory file copy and persists only when every entry succeeds.
  ToolProgressMiddleware can also issue soft hints after a configurable streak
  of successful read/search calls and at configurable total-call intervals so
  the model freezes findings and advances to implementation/verification.
  These counters never block tools or terminate a run.
- Files: `backend/packages/harness/deerflow/sandbox/tools.py` (EDITED),
  `backend/packages/harness/deerflow/agents/middlewares/tool_progress_middleware.py`
  (EDITED), `backend/packages/harness/deerflow/config/tool_progress_config.py`
  (EDITED), `config.example.yaml` (EDITED).
- Tests: `backend/tests/test_str_replace_batch.py` (ADDED),
  `backend/tests/test_tool_progress_middleware.py` (EDITED),
  `backend/tests/test_config_duplicate_keys.py` (ADDED).
- Delete-when: upstream provides atomic same-file edit batches and general
  soft exploration/execution phase budgets with equivalent semantics.
- Upstream status: none sent.

## Patch #73

**Patch #73 - Agent execution and context efficiency controls**

- Class: config-expressed (generic controls with conservative defaults; Atlas
  opts into the routing, reasoning, and run-budget behavior in project config).
- Intent: reduce model round trips and prompt overhead without weakening task
  completion. Adds deterministic high-confidence skill auto-routing, adaptive
  no-thinking follow-ups after deterministic tools, bounded multi-file
  inspect/patch tools, model-call warning/hard caps, post-write hash
  propagation, normalized tool-failure metadata, and a smaller lead prompt.
- Files: lead-agent assembly/prompt, adaptive reasoning and skill-routing
  middleware/config, run limits, read-before-write/result metadata, sandbox
  batch tools, `config.example.yaml` and their focused tests.
- Tests: fork PR #49 required checks, including backend, replay, and workspace
  batch-tool coverage.
- Delete-when: upstream provides equivalent independently configurable controls.
- Upstream status: none sent.

## Patch #74

**Patch #74 - Recursive completion-ledger compaction handoff**

- Class: argus-edit (generic correction to the default summarization behavior;
  an operator custom prompt still overrides it).
- Intent: long implementation runs repeatedly regressed from "ready to write"
  back to repository/database orientation after compaction. The default summary
  is now a recursively merged execution ledger with explicit completed work,
  artifacts/evidence, pending work, exact next action, and do-not-repeat items.
  Durable-context injection marks that ledger as a past-tense handoff rather
  than a new request and directs the agent to continue from the exact next
  action unless newer evidence invalidates completed work.
- Files: `summarization_middleware.py`, `durable_context_middleware.py`,
  `summarization_config.py`, middleware guide.
- Tests: summary prompt contract, prior-ledger merge input, custom prompt
  override, and durable-context continuation contract.
- Delete-when: upstream's default summary is a recursive execution ledger and
  its reinjection contract prevents completed-to-pending phase regression.
- Upstream status: pending replay evidence before proposing upstream.

## Patch #75

**Patch #75 - Preserve active user request in compaction input**

- Class: argus-edit (generic correctness follow-up to patch #74).
- Intent: the latest real user request is deliberately rescued out of the
  compacted message window, but that also hid the full objective and acceptance
  criteria from the summary model. The execution ledger could enumerate
  completed work yet still guess that later phases were undefined. Copy the
  rescued request into the escaped summary input so `ACTIVE OBJECTIVE`,
  `PENDING`, and `EXACT NEXT ACTION` are grounded in the actual current task.
- Files: `summarization_middleware.py`, middleware guide.
- Tests: end-to-end automatic compaction prompt capture proves the rescued
  request is present and XML block delimiters in user content remain escaped.
- Delete-when: the upstream summarizer receives the preserved current request
  or an equivalent durable objective independently of the compaction slice.
- Upstream status: pending canary replay evidence.

## Reverted patch #76

**Patch #76 - Multi-file work-cycle prompt and spill synopsis (reverted)**

- Status: reverted after the 2026-08-31 canary benchmark regressed from 1/3
  passes to 0/3 and increased aggregate input from 1.23M to 1.51M tokens.
- Reason: the activation wording did not eliminate `describe_skill` calls,
  while one run stopped before producing its artifact and another entered a
  21-turn write-recovery loop. The structured `workspace_inspect` synopsis
  worked in isolation but did not produce a reliable end-to-end improvement.
- Revisit only with a narrower patch and a benchmark that passes repeatedly on
  the canary before fleet promotion.

## Reverted patch #77

**Patch #77 - Optional write-file narration (reverted)**

- Status: reverted after the 2026-08-31 canary benchmark matched the rollback
  pass rate (1/3) and increased aggregate input from 1.07M to 1.50M tokens.
- Reason: the schema regression test proved the narrow behavior, but all three
  canary writes supplied narration, so the patch was not exercised end to end.
  The 19-turn outlier instead repeated failed `str_replace` calls. There was no
  measured fleet-performance gain to justify carrying another fork patch.
- Revisit only with production-frequency evidence for omitted narration or a
  deterministic end-to-end scenario that demonstrates net improvement.

## Patch #78

**Patch #78 - Merge model profile and runtime constructor kwargs**

- Class: generic-upstreamable (restores the behavior from upstream fix
  616caa92 / #2017 that a later persistence merge silently overwrote).
- Intent: `create_chat_model()` previously expanded runtime kwargs and model
  profile settings separately into the provider constructor. A model with a
  `when_thinking_disabled.reasoning_effort` fallback therefore crashed before
  its first request whenever the Web UI also supplied `reasoning_effort`.
  Merge the dictionaries before expansion and let the explicit runtime value
  override the profile fallback.
- Files: `backend/packages/harness/deerflow/models/factory.py` (EDITED), model
  factory guide and root README (EDITED).
- Tests: `backend/tests/test_model_factory.py` reproduces the collision and
  verifies runtime precedence.
- Delete-when: upstream restores the merge-before-expansion behavior on the
  branch Argus next rebases onto.
- Upstream status: prior upstream fix regressed; send the regression test and
  restoration upstream.

## Patch #79

**Patch #79 - Restore agents gallery navigation; drop in-UI agent creation**

- Class: argus-edit (frontend-only edits to upstream workspace files).
- Intent: fork patch #65 removed the sidebar "Agents" entry, which was the
  only navigation path to `/workspace/agents` - the gallery that also hosts
  the per-agent settings dialog and the "New Agent" button. The removal was
  meant to retire in-UI custom-agent creation, but it also orphaned viewing
  and editing of existing agents: `workspace-nav-chat-list.tsx` no longer
  linked anywhere into the agents segment, so citizens could not reach their
  agents' cards, edit their settings, or open a chat with a non-default agent
  (only `atlas` is reachable via the nginx/console hardcoded entry points).
  This patch (1) re-adds the feature-gated "Agents" sidebar entry (restoring
  the pre-#65 block incl. the disabled-tooltip state from feature-gating
  commit 21b35102), (2) removes the two "New Agent" buttons from
  `agent-gallery.tsx` so creation is done through the stack's GitHub
  repository (`config.yaml` + `SOUL.md`, commit, merge) instead of the web
  UI, (3) redirects the old `/workspace/agents/new` wizard to the gallery so
  stale links and bookmarks land somewhere useful, and (4) aligns the gallery
  copy (en + zh) with the repo-driven creation flow. View/edit/delete of
  existing agents is unchanged (`AgentCard` settings dialog and delete stay).
  Agent creation remains possible via the `/api/agents` routes (still gated
  on `agents_api.enabled`, which is also the view/edit gate) - UI-only
  retirement, matching the "through GitHub" policy.
- Files: `frontend/src/components/workspace/workspace-nav-chat-list.tsx`
  (EDITED), `frontend/src/components/workspace/agents/agent-gallery.tsx`
  (EDITED), `frontend/src/app/workspace/agents/new/page.tsx` (EDITED to a
  redirect), `frontend/src/core/i18n/locales/en-US.ts` (EDITED),
  `frontend/src/core/i18n/locales/zh-CN.ts` (EDITED),
  `frontend/tests/e2e/sidebar.spec.ts` (EDITED),
  `frontend/tests/e2e/ui-polish-mobile.spec.ts` (EDITED).
- Tests: `frontend/tests/e2e/sidebar.spec.ts` and
  `frontend/tests/e2e/ui-polish-mobile.spec.ts` updated to assert the restored
  Agents entry (desktop + mobile) against the default agents-api-enabled
  mock; `agents-feature-disabled.spec.ts` still covers the disabled state and
  the no-API-call gate (unchanged).
- Delete-when: not tied to an upstream regression; revisit if upstream adds a
  first-class agents page/nav that we can adopt instead of carrying the
  gallery nav entry.
- Upstream status: not upstreamable as-is (the copy and the exact affordances
  are Argus policy); the separate gallery/view surfaces are upstream code so
  this is pure carry cost.

## Dropped / deferred / re-expressed (v2.0.0 rebase record - do not re-add blindly)

**Dropped as upstream-subsumed (verified during the 2026-06-29/30 rebase):**

- **#8 `langgraph_auth` lazy-init** (numbered #9 in the pre-v2 PATCHES.md):
  dead since Argus moved off standalone `langgraph dev` to the gateway
  runtime; the fork had already self-dropped it 2026-06-03. Do not revive
  unless we run `langgraph dev` standalone again.
- **#17 agent-dir-fallback** (shared-dir fallback when the per-user dir has
  only `memory.json`): upstream fixed it better (upstream issue #3390).
- **`supports_streaming` override**: now native upstream. **CORRECTION
  2026-07-01: this override is NOT patch #10.** The rebase note originally
  recorded "#10 supports_streaming override, dropped" - a mislabel that
  silently regressed Telegram artifact delivery until 762b61eb re-wired the
  presenter. Patch #10 (the Telegram artifact presenter) is alive; see its
  section. Do not treat #10 as obsolete on the next sync.
- **3 pre-2026 loop-detector patches** (nudge-toward-observation,
  edit-aware-reset, layer-2 frequency drop): subsumed by upstream's
  warning-queue architecture back in the 2026-05-28 upgrade; if Qwen loop
  behavior regresses, re-tune against the current architecture.

**Deferred (do not port as-was):**

- **#27 agent sub-component pooling** (perf-only half of the old #27; the
  fire-and-forget emoji half lives on in the telegram chain): its cache key
  cannot capture v2.0.0's deferred-tools subsystem inputs, so porting it
  risks stale prompts for zero behavior change. If the perf matters,
  re-derive against the `assemble_deferred_tools` /
  `build_middlewares(deferred_setup=...)` shape.

**Re-expressed as config fields (no longer constant patches):** see #2 and #3.

**Not carried - verify on next sync (suspected silent drops, like #10 was):**

- **#19 agent-chat model precedence** (3 pre-v2 frontend commits: per-thread
  override, else the agent's pinned model, no global last-pick bleed): absent
  from `v2.0.0..2df36c99` and NOT visibly upstream-subsumed - at tip, the
  agent chat page still injects only `agent_name`
  (`frontend/src/app/workspace/agents/[agent_name]/chats/[thread_id]/page.tsx`,
  `context: { ...settings.context, agent_name }`) and InputBox still gates
  modes on `supports_thinking` via `context.model_name`. If glm-planner mode
  gating is broken again in the UI, re-port as a new numbered patch.
- **#12 sandbox Created-but-not-Running detection**: absent from the carry;
  upstream v2.0.0 ships sandbox orphan reconciliation and #33's network mode
  removes the port-bind root cause. Presumed subsumed; confirm if sandboxes
  ever hang in Created again.

---

## Carry budget ledger

Re-measure at every sync (`git diff --shortstat <upstream-base>..argus` plus
the upstream-file edit split). The goal is that these numbers go DOWN over
time as patches are upstreamed or subsumed; a rising channels number means the
telegram subsystem needs the FORK-REVIEW lever-1 treatment (upstream the
design or move it behind an extension point).

| Date | Base | Commits | Files | Lines | Upstream-file edited lines |
|---|---|---|---|---|---|
| 2026-07-01 | v2.0.0 -> 2df36c99 | 29 | 85 | +6168 / -812 | ~1600 (~1460 in `app/channels/`) |
| 2026-07-02 | v2.0.0 -> #40 tip | 32 | 90 | +7433 / -668 | app-code excl. tests/docs: 1923 (776 in `app/channels/`, was 1099); tests: 1350. #40 cut `telegram.py` 574 -> 251 |
| 2026-09-02 | bytedance/main 3a967d4f (2026-08-15) -> c58d6168 (#79) | 140 | 240 | +17512 / -1740 | app-code excl. tests/docs: 5957 (802 in `app/channels/`); tests: 3389. Measured against the merge-base with `bytedance/main` (v2.0.0 sits on `2.0.x-dev`, not `main`); over the 2,500 alarm, see acropolis docs/DEERFLOW-SYNC.md |

Methodology note (2026-07-02): the last column is now measured against the
`v2.0.0` tag over files that exist at v2.0.0 (insertions+deletions), split
app-code vs tests. The 2026-07-01 row's ~1600/~1460 came from FORK-REVIEW's
2026-06-30 measurement against merge-base 2ace78d1 with a different file
scope and is not directly comparable; like-for-like against v2.0.0 the
pre-#40 tip was 2246 app-code (1099 in `app/channels/`). Reproduce with:
`git diff --numstat v2.0.0 | while read a d f; do git cat-file -e
"v2.0.0:$f" 2>/dev/null && echo "$a $d $f"; done | awk '...'`.

## Patch #80

**Patch #80 - Sandbox hardening knobs: limits, capabilities, seccomp, no-new-privileges**

- Class: generic-upstreamable (config-driven, off by default; upstream behaviour
  is reproduced exactly when nothing is configured).
- Intent: `LocalContainerBackend._start_container` launched every agent sandbox
  with a single hard-coded `--security-opt seccomp=unconfined` and no resource
  limits, so untrusted agent code ran as an unlimited container with the
  kernel's syscall filter switched off (live on Argus 2026-09-03: `Memory=0
  PidsLimit=0 CapDrop=[] SecurityOpt=[seccomp=unconfined]` on every
  `argus-*-sandbox-*`; Cerberus `sandbox_unhardened` + `seccomp_unconfined`).
  `SandboxConfig` gains `memory`, `pids_limit`, `cpus`, `cap_drop`, `cap_add`,
  `seccomp_profile`, `no_new_privileges` and `extra_run_args`; the provider
  forwards them and the backend renders them on the docker/podman path
  (`_docker_hardening_args`). `seccomp_profile` unset keeps upstream's
  `seccomp=unconfined`; `"default"` passes no seccomp option (the runtime's
  own default filter); any other value is a profile path. Apple `container`
  has none of these flags and is left untouched. Verified on the production
  image (`all-in-one-sandbox@sha256:742062f9`) under `--memory 3g
  --pids-limit 1024 --cpus 8 --security-opt no-new-privileges` with the
  default seccomp filter: shell exec, cgroup limits visible inside, browser
  info + screenshot (chromium renders) all fine.
- Files: `backend/packages/harness/deerflow/config/sandbox_config.py`
  (EDITED: eight fields + docstring),
  `backend/packages/harness/deerflow/community/aio_sandbox/local_backend.py`
  (EDITED: constructor kwargs, `_docker_hardening_args`, call site),
  `backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py`
  (EDITED: `_load_config` keys, `_create_backend` forwarding).
- Tests: `backend/tests/test_aio_sandbox_local_backend.py` (seven new tests:
  unconfigured == upstream, `default` omits seccomp, custom profile passed
  through, limits/caps/no-new-privileges/extra args rendered before the
  image, fractional cpus, blank knobs ignored, Apple runtime untouched);
  `backend/tests/test_aio_sandbox_provider.py` (three new tests: defaults,
  knobs carried by `_load_config`, `_create_backend` forwards them).
- Delete-when: upstream exposes equivalent sandbox resource/security options
  on `SandboxConfig` (none as of the 2026-08-15 base).
- Upstream status: none sent yet; the shape is generic (every field maps to a
  documented docker flag) and is a candidate for the next upstream batch.

## Patch #81

**Patch #81 - Bash inspection/execution command classification library**

- Class: argus-additive (new module) with a small argus-edit (sandbox_audit import swap).
- Intent: Move quote- and heredoc-aware shell compound-command splitting helpers out of
  `SandboxAuditMiddleware` into a neutral location `deerflow.sandbox.command_classify`,
  and introduce `classify_bash_command` to classify bash commands as pure-read ("inspection")
  versus state-modifying ("execution") or "unknown" (empty/whitespace). This enables efficiency
  steering and loop-detection/progress-tracking heuristics (follow-up patch #82) without
  importing from `deerflow.agents.middlewares`.
- Files: `backend/packages/harness/deerflow/sandbox/command_classify.py`
  (NEW: `split_compound_command`, `classify_bash_command`, and helpers),
  `backend/packages/harness/deerflow/agents/middlewares/sandbox_audit_middleware.py`
  (EDITED: import `split_compound_command` from `deerflow.sandbox.command_classify`),
  `backend/packages/harness/deerflow/sandbox/AGENTS.md` (EDITED: documentation),
  `backend/packages/harness/deerflow/agents/middlewares/AGENTS.md` (EDITED: documentation).
- Tests: `backend/tests/test_command_classify.py` (new unit tests covering splitting,
  fail-closed/fail-open behaviors, pure-read binaries, sed/sort/find mutating flags, redirection
  sinks/exceptions, git inspection subcommands, env prefixes, and pipelines);
  `backend/tests/test_sandbox_audit_middleware.py` (267 existing tests pass unchanged).
- Delete-when: upstream grows an equivalent shared shell-classification helper.
- Upstream status: none sent yet.

## Patch #82

**Patch #82 - Bash inspection wiring for ToolProgress streak and loop-detection Layer 2**

- Class: config-expressed
- Intent: Wire bash command classification (`deerflow.sandbox.command_classify`, patch #81)
  into `ToolProgressMiddleware` and `LoopDetectionMiddleware` to eliminate efficiency blindspots
  where micro-peeking loops through `bash` evade read/write streak accounting and per-tool frequency caps.
  Config-gated off by default to maintain byte-identical upstream behavior.
- Files:
  `backend/packages/harness/deerflow/config/tool_progress_config.py` (EDITED: `bash_inspection_counts_as_read` field),
  `backend/packages/harness/deerflow/agents/middlewares/tool_progress_middleware.py` (EDITED: wire bash inspection to advance read_only_streak),
  `backend/packages/harness/deerflow/config/loop_detection_config.py` (EDITED: document subcategory override keys),
  `backend/packages/harness/deerflow/agents/middlewares/loop_detection_middleware.py` (EDITED: Layer-2 subcategory tracking and steering messages),
  `backend/packages/harness/deerflow/agents/middlewares/AGENTS.md` (EDITED: entries 12 and 28 documentation),
  `config.example.yaml` (EDITED: commented-out example configuration for both knobs).
- Tests:
  `backend/tests/test_tool_progress_middleware.py` (added unit tests for flag off/on, inspection vs execution bash, failed commands, non-bash tools, JSON-string args);
  `backend/tests/test_loop_detection_middleware.py` (added unit tests for default config no-op, subcategory warn/hard-stop without window dilution, execution ignore, mixed streams, warn-once semantics, LRU eviction and reset cleanup);
  `backend/tests/test_loop_detection_config.py` (added test for subcategory override validation).
- Delete-when: upstream adopts unified shell command classification and efficiency tracking for shell inspection.
- Upstream status: none sent yet.

## Patch #83

**Patch #83 - Truthful meta-classify for wrapper errors; bash.inspection reset semantics**

- Class: config-expressed
- Intent: Fix two production defects observed during the patch #82 canary:
  1. `tool_result_meta.py`: narrow error classification on tool-wrapper errors (`Error invoking tool '...' with kwargs {...} with error:`)
     to the trailing error text only, preventing echoed kwargs (e.g. file content containing `disabled`) from poisoning classification into false terminal blocks.
  2. `loop_detection_middleware.py`: trim subcategory history deque to the subcategory's own hard limit rather than the inflated global window,
     clear the subcategory counter when its hard stop fires, and reset the subcategory counter upon write progress (`write_file`, `str_replace`, or execution `bash`).
- Files:
  `backend/packages/harness/deerflow/agents/middlewares/tool_result_meta.py` (EDITED: narrow error classification on tool wrapper messages),
  `backend/packages/harness/deerflow/agents/middlewares/loop_detection_middleware.py` (EDITED: subcategory own-limit trimming, hard-stop reset, and write-progress reset),
  `backend/packages/harness/deerflow/agents/middlewares/AGENTS.md` (EDITED: entry 28 reset and trim semantics),
  `config.example.yaml` (EDITED: comment notes on bash.inspection reset and trim semantics).
- Tests:
  `backend/tests/test_tool_result_meta.py` (added regression tests for echoed kwargs poisoning, nested wrapper delimiters, genuine config errors in trailing text, non-wrapper fallback);
  `backend/tests/test_loop_detection_middleware.py` (added tests for write progress resetting subcat counter, pure inspection hard stop, hard stop resetting counter, subcategory deque trimming to own limit).
- Delete-when: upstream adopts structured error classification and subcategory frequency window decay.
- Upstream status: none sent yet.


