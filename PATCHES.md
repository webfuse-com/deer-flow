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
| [#44](#patch-44) | unattended-silence: no blank-final retry, wider narration backstop, no token logging | argus-edit | this PR |
| [#45](#patch-45) | delivery-report callback for scheduled playbook fires | argus-additive | this PR |
| [#46](#patch-46) | tool_search.exclude: deferral opt-out for hot MCP tools | argus-edit | this PR |
| [#47](#patch-47) | per-lead-model summarization overrides | argus-edit | f797b8b8, 81e59e14 |
| [#48](#patch-48) | fail-closed Pythia retrieval ring (opt-in, not opt-out) | argus-edit | 6d6eda1c |
| [#49](#patch-49) | omitted-item index in list-shaped tool-output previews | argus-edit | this PR |

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
  0-10s polling delay (#28, config-gated, polling stays as fallback); grafts
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
  `backend/tests/test_csrf_middleware.py` (EDITED, webhook-exemption regression tests)
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
- Amended 2026-07-31: the `isLastGroup` rule alone was too broad. A run
  stopped mid-subtask and then reloaded from history has exactly the
  protected shape (last group, not loading, no ToolMessage), so the card
  spun "Running subtask" forever instead of resolving. Upstream's e2e
  `subtask-card.spec.ts` "shows failed after a stopped task thread is
  reloaded" asserts the correct behaviour and had been failing since this
  patch landed on 2026-07-03 - unnoticed because the e2e job is not in the
  required set and had not run since 2026-07-01. The `in_progress` branch
  now also requires `hasStreamedThisSession`, latched in `message-list.tsx`
  the first time `thread.isLoading` is true. A transient loading=false
  during a live turn keeps the latch set (patch intent preserved); a thread
  only ever loaded from history never sets it, so a genuinely stopped
  subtask resolves to `"failed"`.
- Files: `frontend/src/core/tasks/subtask-result.ts` (added
  `isLastGroup` + `hasStreamedThisSession` params + JSDoc),
  `frontend/src/components/workspace/messages/message-list.tsx` (pass
  `isLastGroup` from the group index, plus the `hasStreamedRef` latch),
  `frontend/tests/unit/core/tasks/subtask-result.test.ts` (updated
  existing test to pass `false`, added tests for the live-turn and
  stopped-thread cases)
- Tests: `frontend/tests/unit/core/tasks/subtask-result.test.ts` (+2:
  "stays in_progress for the last group even when loading is briefly false",
  "fails the last group of a thread that never streamed this session";
  updated: "does not revive an earlier unfinished task" now passes
  `isLastGroup=false`). Upstream e2e `subtask-card.spec.ts` covers the
  stopped-thread case end to end and must stay green.
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

**Patch #44 - unattended-silence: no blank-final retry, wider narration
backstop, no token logging**

- Class: argus-edit (edits the #37 middleware, the #34 heuristic in
  `app/channels/manager.py`, and the gateway logging bootstrap)
- Intent: three fixes from the 2026-07-18/19 hourly-Telegram-spam incident.
  (1) Patch #37's `EmptyFinalRetryMiddleware` retried blank finals on EVERY
  turn; on an unattended (scheduled-playbook) turn a blank final — or the
  `.` no-op sentinel the playbook prompts now ask for — is the *desired*
  outcome (the #31/#32/#34 silence branch suppresses the send), so the
  retry re-sampled the model into narrating ("No meetings in the
  window...") and turned compliant silence into hourly channel noise. The
  middleware now reads `unattended` from `request.runtime.context` (the
  same signal the #30 memory write policy uses) and returns the blank
  without retrying; attended turns keep retry-once byte-for-byte.
  (2) The #34 narrated-silence backstop capped announcements at 120 chars;
  the real narrations ran to ~250 chars (three-sentence monologues) and
  sailed through hourly. Cap is now 280, "calendar/schedule is clear" and
  "nothing ... attention" count as announcement phrasing, and a
  contrast/alert-marker veto ("but", "however", "urgent", "moved",
  "cancelled", ...) takes over from the cap as the protection for genuine
  content behind an announcement-shaped opener.
  (3) httpx logs every request URL at INFO, so the Telegram bot token
  appeared in the journal in plaintext on every send; the gateway
  bootstrap now pins `httpx`/`httpcore` to WARNING (`apply_logging_level`
  only adjusts the deerflow/app hierarchies, so the pin survives the
  lifespan log-level override).
- Files:
  `backend/packages/harness/deerflow/agents/middlewares/empty_final_retry_middleware.py`
  (EDITED, +`_is_unattended` + skip branch),
  `backend/app/channels/manager.py` (EDITED, #34 heuristic: cap 120→280,
  +2 announcement alternates, +`_SILENCE_VETO_RE`),
  `backend/app/gateway/app.py` (EDITED, +httpx/httpcore WARNING pin)
- Tests: `backend/tests/test_empty_final_retry.py` (+`TestIsUnattended`,
  +`TestUnattendedNeverRetries` incl. an attended-still-retries regression
  guard; `_req` now models an explicit attended runtime),
  `backend/tests/test_unattended_silence.py` (+6 verbatim incident
  narrations that must blank, +5 veto cases that must survive)
- Delete-when: (1) goes with patch #37 if the blank-final root cause is
  fixed upstream. (2) goes with the #31/#32/#34 silence branch when
  scheduled runs get a first-class delivery contract (the playbook fire
  reporting `silent` instead of the model posting anything at all).
  (3) goes if upstream adopts a redacting log filter for channel HTTP
  clients.
- Upstream status: none. (3) is generic-upstreamable as a standalone
  "don't log bot tokens" fix; (1)/(2) depend on the argus-only scheduled
  playbook machinery.

---

## Patch #45

**Patch #45 - delivery-report callback for scheduled playbook fires**

- Class: argus-additive (new `_delivery_report.py`; one field on
  InboundMessage/PlaybookFireRequest; four call sites at named seams in
  `manager.py`)
- Intent: Chronos fires a playbook with a per-run ``report_url`` and has
  handled a ``delivered|silent|failed`` callback since its patch-#30-era
  contract (``scheduler/src/routes/api.py::report_run_output``) — but the
  gateway never called back, so every ``channel_notify`` run sat ``running``
  for CHRONOS_DELIVERY_REPORT_TIMEOUT_SEC (1800s) and closed as
  ``ok/unreported``; the dashboard could not tell a delivered briefing from
  a suppressed-silent tick (patch #31/#32/#34/#44) or a failed turn. The
  fire endpoint now forwards ``report_url`` onto the InboundMessage and the
  channel manager POSTs the outcome once the async turn resolves:
  ``silent`` from both unattended-suppression branches, ``delivered`` after
  the outbound publish (with the message text), ``failed`` on a captured
  stream error or a thread-busy rejection. One POST, one retry, never
  raises — a report failure degrades to exactly the pre-#45 unreported
  behavior. Interactive turns and fires without ``report_url`` are
  byte-for-byte unchanged.
- Files: `backend/app/channels/_delivery_report.py` (NEW),
  `backend/app/channels/message_bus.py` (EDITED, +field),
  `backend/app/gateway/routers/playbooks.py` (EDITED, +request field,
  +forward), `backend/app/channels/manager.py` (EDITED,
  +`_report_unattended_outcome` + 4 call sites)
- Tests: `backend/tests/test_delivery_report.py` (NEW: payload shape,
  internal-token header, retry-once/never-raise, message-text cap, manager
  gate), `backend/tests/test_playbook_fire.py` (EDITED, +2:
  report_url flow-through, default-None)
- Delete-when: the schedule system is re-expressed on a native scheduling
  API where the scheduler owns the turn lifecycle end-to-end (no async
  fire-and-deliver seam to report across).
- Upstream status: none (argus-additive; depends on the argus-only
  playbook fire endpoint and Chronos).

---

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

Methodology note (2026-07-02): the last column is now measured against the
`v2.0.0` tag over files that exist at v2.0.0 (insertions+deletions), split
app-code vs tests. The 2026-07-01 row's ~1600/~1460 came from FORK-REVIEW's
2026-06-30 measurement against merge-base 2ace78d1 with a different file
scope and is not directly comparable; like-for-like against v2.0.0 the
pre-#40 tip was 2246 app-code (1099 in `app/channels/`). Reproduce with:
`git diff --numstat v2.0.0 | while read a d f; do git cat-file -e
"v2.0.0:$f" 2>/dev/null && echo "$a $d $f"; done | awk '...'`.


## Patch #46

**Patch #46 - tool_search.exclude: deferral opt-out for hot MCP tools.**

Deferral (tool_search.enabled) is all-or-nothing over MCP-tagged tools. The
Argus capability rewrite measured the cost on the canary (goldset-wide-v2,
15 runs/arm, 2026-07-23): the promotion round-trip doubled mean wall clock
(13.0s -> 26.3s) because the HOT knowledge tools (pythia_query, kb_query,
entity_search, code_search_*) are used in most turns - their schemas cost
less context than the extra local-27B round costs latency. The placement
rubric wants hot tools bound and only the long tail (30 atlas control-plane
tools, future integration suites) deferred.

`ToolSearchConfig` gains `exclude: list[str]` - fnmatch patterns matched
against FINAL (server-prefixed) tool names. Excluded MCP tools skip the
deferred catalog and stay always-bound; the assemble fail-closed guard
ignores excluded tools (all-excluded is a valid empty setup, not an error).
Threaded through all four assemble_deferred_tools call sites (lead x2,
embedded client, subagent executor).

Exit condition: upstream grows per-server or per-tool deferral control in
extensions_config/tool_search config; migrate the exclude list there and
drop this patch.

Tests: backend/tests/test_deferred_setup.py::TestExclude (4).

## Patch #47

**Patch #47 - per-lead-model summarization overrides.**

`summarization` is a single global block, but both the correct trigger and the
correct summarizer depend on the LEAD model's context window. Two independent
failures follow from that:

1. **The window is wasted.** An absolute `tokens` trigger tuned for a 131k model
   fires at ~9% of a 1M-context model's window. Measured on argus (LiteLLM
   `/model/info`): `local-qwen` max_input 131,072, `glm-nw` 1,048,560. The
   global trigger is 91,750 = 70% of local-qwen. A `glm-planner` run (lead model
   `glm-nw`) therefore summarized at 8.7% of its window, discarding ~91% of the
   context the operator is paying for.
2. **The summarizer cannot read what it compresses.** The global
   `model_name: local-qwen` (131k) was asked to summarize a thread that may hold
   up to 1M tokens. The compression is silently lossy; nothing surfaces it.

`fraction: 0.7` is the natural upstream expression of this and does NOT work
here: LangChain's parent middleware needs a model profile to resolve a fraction,
and our LiteLLM aliases carry none (`ModelConfig` declares no
max_input_tokens/context_window at all), so it raises ValueError on init. Per-agent
config is also not a lever: `AgentConfig` has no summarization field.

`SummarizationConfig` gains `per_model: dict[str, SummarizationOverride]` plus
`resolved_for(lead_model_name)`. Only fields set on the override apply; the rest
are inherited. An absent key returns `self` unchanged, so every existing config
behaves exactly as before. The resolved view drops `per_model` so a second
resolution cannot compound.

`_create_summarization_middleware` gains `lead_model_name: str | None = None` and
`build_middlewares` passes the already-resolved `model_name` (the same value that
gates ViewImageMiddleware two blocks below), so no new resolution logic is
introduced. Passing None preserves the pre-patch behavior exactly.

Implementation note worth keeping: `resolved_for` copies the override's field
VALUES, not `model_dump()`. Dumping converts the nested `ContextSize` models into
plain dicts, and `model_copy(update=...)` would then inject dicts where the
factory calls `.to_tuple()` on them - an AttributeError at graph-build time.
Caught by the tests before it ever ran.

Exit condition: upstream adds a model profile / context_window to ModelConfig so
`fraction` triggers resolve for arbitrary providers, OR upstream grows per-model
summarization config. Then move the ratios there and drop this patch.

Tests: backend/tests/test_summarization_per_model.py (16) - resolution semantics
(inherit, no-mutation, no-recursion, list triggers, empty override), the factory
seam (a glm-nw lead selects the glm-nw summarizer; a model without an override
keeps the global one), and the wiring guard that `build_middlewares` passes
`lead_model_name=model_name` rather than a literal.

## Patch #48

**Patch #48 - fail-closed Pythia retrieval ring (opt-in, not opt-out).**

Deterministic company-KB retrieval (`PythiaRetrievalMiddleware`, patch #30) was
gated FAIL-OPEN: an agent that declared no `pythia_ring` in its `config.yaml`
inherited ring `"internal"` from the stack env flag `PYTHIA_ROUTER_INJECT`. Two
independent defaults pointed the same way - the `build_middlewares` gate
(`elif _stack_flag_on: _ring = "internal"`) and the middleware constructor
(`def __init__(self, ring: str = "internal")`, plus `enabled = <known ring> OR
flag_on`, which enabled even an unrecognised ring string).

That is the wrong default for an opt-in capability, and it silently reversed a
deliberate opt-out in production. A turn that names no agent runs as the
synthetic agent `"default"` (`agent_name or "default"` in the same module), and
there is no `default` entry under `agents/`, so `load_agent_config` raises,
`_agent_config` is None, `getattr(..., "pythia_ring", None)` is None, and the
turn got internal-ring company knowledge injected. Observed on two argus stacks
whose real agent sets `pythia_ring: none` with the comment "agent-driven
retrieval": UI threads were served 6 blocks of unrelated company knowledge per
turn, and the model opened its replies by dismissing its own injected context
("Those Pythia results are unrelated noise. Let me give you the real answer."),
on questions with no company-knowledge dimension at all. Cost: ~0.5-1.0s of
added latency per turn (one observed call timed out at 6030ms and was retried),
context spent on irrelevant blocks, and a model being trained by its own harness
to distrust its inputs.

Now: retrieval attaches only when an agent OPTS IN by declaring `pythia_ring`.
Absent, empty, `"none"`, or unrecognised all mean no retrieval, in both the gate
and the constructor. `PYTHIA_ROUTER_INJECT` (legacy alias
`PYTHIA_RETRIEVAL_ENABLED`) is demoted to a KILL SWITCH: a false value
(`0`/`false`/`no`/`off`) disables retrieval stack-wide even for agents that opt
in, and leaving it unset enables nothing. The gate logs at INFO when it drops an
opted-in agent's ring because of the kill switch, so a disabled stack is not
silent.

Operator-visible consequence: on a stack that relied on the flag alone to give
every agent retrieval, agents must now name their ring. On argus this is a no-op
for the two KB agents (`pythia-internal` -> `internal`, `pythia-ext` ->
`external`, both already explicit fleet-wide) and intentionally turns retrieval
OFF for `default` and for any agent that never declared one.

Exit condition: upstream grows a first-class per-agent retrieval capability with
its own gating, or `pythia_ring` moves into a shared capability config. Then the
ring resolution moves there and this patch drops.

Tests: backend/tests/test_pythia_retrieval_middleware.py - `TestRingGating`
gains the fail-closed constructor cases (unknown ring, absent ring, empty ring,
the kill switch across `0`/`false`/`no`/`off` + the legacy alias) replacing
`test_stack_flag_enables_even_unknown_ring`, which asserted the old fail-open
contract; and a new `TestLeadAgentWiring` class covers whether the middleware is
ATTACHED AT ALL - the module docstring already claimed that coverage and did not
have it, which is exactly where the bug lived. It includes the production path
(the synthetic `default` agent with no config entry) and the opt-in paths. 13 of
the 28 tests in the file fail against the pre-patch source.

## Patch #49

**Patch #49 - omitted-item index in list-shaped tool-output previews.**

The tool-output budget middleware replaces an externalized result with a
head+tail preview. For LIST-shaped results (blocks joined with `\n\n---\n\n`,
the join every kb-api MCP listing tool uses) that shape is the worst case: the
preview keeps the first and last item, silently drops every middle one, and
still reads like a complete listing. The model has no cue that anything
between head and tail existed, so "absent from the preview" becomes "absent
from the source". That is the 2026-08-03 daily-review incident on
atlas-nicholas: a 22,636-char `pythia_list_meetings` result held four
meetings; the two 1:1s in the middle vanished from the preview and the review
reported their minutes as not captured and "not recoverable" while both sat in
Pythia with full summaries.

`_omitted_block_headers(content, head_end, tail_start)` scans the block starts
of a separator-joined result and returns the first line of every block whose
header offset falls inside the omitted span (including a block whose body the
tail shows but whose header was cut). `_build_preview` appends those lines to
the existing file-reference marker as an index: "The omitted span contains N
list item(s) whose first lines are: ...". Bounded by 24 items x 160 chars plus
a "(+N more)" overflow marker, so a pathological result cannot grow the
preview unbounded. Content without the separator returns ([], 0) and the
preview stays byte-identical to pre-patch output; `_build_fallback` is
deliberately untouched because its contract is a hard max_chars guarantee.

Exit condition: upstream's budget middleware grows structure-aware previews
(or per-item summarization) for list results. Then drop the helper and the
index note.

Tests: backend/tests/test_tool_output_budget_middleware.py
`TestBuildPreviewOmittedBlockIndex` (6) - middle items indexed, the incident
shape (a four-meeting listing must surface both middle 1:1 headers), non-list
content byte-identical, all-headers-visible means no index, cap + "(+N more)"
marker, long header truncation. 4 of the 6 fail against the pre-patch source
(the two absence assertions pass both ways by design, locking no-regression).
Full file: 101 passed; tests/test_tool_output_truncation.py: 36 passed.

## Patch #51
**Patch #51 - inline connector prompts on the playbook fire endpoint**

- `POST /api/playbooks/{id}/fire` accepts an optional `prompt_text`. Chronos
  assembles a connector's pinned PROMPTS text plus the gated
  `[transformer-data]` frame and sends it inline, because connector hooks
  (`ctx.run_agent`) have no `config/atlas-playbooks/<id>.md` file to read.
  When set, the file lookup is skipped and the path id is used for
  logging/attribution only; when absent, behavior is byte-identical to #30.
- Guards: whitespace-only prompt_text is 422 (a caller bug, never a silent
  fall-through to the file), and >64KB is 422 (Chronos's data gate caps the
  machine-data frame at 32KB, so anything near this bound is a bug). Date
  placeholders ({{TODAY}}, {{THIS_MONDAY}}) expand on both paths.
- Trust boundary unchanged: the endpoint already requires the internal
  service token, and the token holder that sends prompt_text (Chronos) is the
  same component that pins the prompt at connector-deploy time and sanitizes
  the data frame. No SSO-session caller can reach this route.
- Files: backend/app/gateway/routers/playbooks.py,
  backend/tests/test_playbook_fire.py
- Tests: TestFirePromptText (7 tests; 5 fail against the pre-patch source,
  2 lock no-regression of the file path by design).
- Delete when: connector prompts are reconciled into the stack's playbook
  dir like schedules are, or upstream grows an equivalent inline surface.

## Patch #52
**Patch #52 - scheduled fires deliver to root chats only**

- `fire_playbook` iterated `store.list_entries(channel)`, which returns EVERY
  stored mapping — including per-topic thread rows (`channel:chat:topic`,
  e.g. the `hook:<connector>:<prompt>` threads that connector intents create
  under patch #51). One topic thread in the store made every scheduled
  playbook fire N times into the same chat: the copies coalesced into an
  N-fold prompt, the unattended silent-turn suppression was lost, and the
  citizen saw hourly "(No response from agent)" for playbooks that are
  silent by design (found live on atlas-nicholas within two hours of the
  first hook thread existing).
- Fix: filter to root rows (`not entry.get("topic_id")`); root rows are
  unique per chat by key construction. A store holding ONLY topic threads is
  the same as no mapping: 409.
- Observed but not fixed here: the inbound coalesce path merged the two
  unattended copies and the empty-final retry ran on the merged turn even
  though patch #44 skips unattended retries — the unattended flag appears
  not to survive coalescing. Unreachable for scheduled fires once this
  patch is in (single message, nothing to coalesce); worth its own look if
  coalescing ever applies to unattended traffic again.
- Files: backend/app/gateway/routers/playbooks.py,
  backend/tests/test_playbook_fire.py
- Tests: TestFireTargetsRootChatsOnly (2; both fail pre-patch).
- Delete when: the store separates chat mappings from topic thread mappings.

## Patch #50
**Patch #50 - connector (was: transformer) call proxy for apps**

- 2026-08-06: renamed to /api/connectors/* to match the browser vocabulary
  (Agora + Console both say "connectors"). /api/transformers/* stays served
  by `legacy_router` — apps published before the rename hard-code the old
  path in their JavaScript and are static files that will not update
  themselves. csrf_middleware exempts BOTH prefixes.
- 2026-08-06 (app-origin isolation): the app tier moved to its own origin,
  `apps-<citizen>.acro.surfly.com`, so an agent-authored page no longer
  carries the citizen's session. Connector calls are therefore cross-origin,
  and this route now answers its own CORS preflight and stamps
  Access-Control-Allow-Origin for `apps-*` origins only.
  **Access-Control-Allow-Credentials is deliberately never set** — the whole
  point is that the app cannot get the session back. Identity still reaches
  the connector: Caddy asserts X-Auth-Email server-side and it returns as
  `called_by`. Deliberately NOT wired through GATEWAY_CORS_ORIGINS, whose
  middleware sets allow_credentials=True and would undo the isolation.
  Error paths raise through a helper that carries the CORS headers, or a
  failing call reaches the browser as an opaque CORS error instead of its
  real status.
  Also required: `auth_middleware` now lets OPTIONS through. A browser sends
  preflights with no cookie by specification, so gating them 401s every
  cross-origin call before routing. OPTIONS changes no state and the route's
  preflight handler only echoes origins it recognises.

- Class: argus-additive (new router)
- Intent: Apps deployed at /app/<slug>/ are same-origin with /api/*, so
  JavaScript in an app can fetch a transformer function. This route proxies
  to Chronos's call surface (server-to-server on argus-net). No
  X-Transformer-Key header needed from JS; the gateway authenticates to
  Chronos with SCHEDULER_API_KEY (same key every gateway already holds).
- Multi-citizen: when citizen B opens citizen A's app and the JS calls
  this route, it hits citizen A's gateway. The transformer runs on A's
  stack with A's credentials. The caller's SSO email is logged for audit.
- Files: backend/app/gateway/routers/transformers_proxy.py (NEW),
  backend/app/gateway/app.py (EDITED: import + include_router)
- Tests: TODO (integration test: deploy a ping transformer, call from
  an app, verify result)
- Delete-when: upstream DeerFlow grows its own app-backing API surface.
- Upstream status: none.
