# Argus patches on top of upstream DeerFlow

This fork (`webfuse-com/deer-flow`, branch `argus`) carries a small set of
patches on top of `bytedance/deer-flow` `main`. This file is the source of
truth for *what* we carry and *why*, so fork drift stays legible and every
patch has a documented exit condition.

**Maintenance rule:** every patch here must (a) have a one-line `[argus]`
commit subject, (b) ship with a test where behavior changed, (c) have a
"delete-when" condition. If a patch can be upstreamed, open the PR and note
it. When upstream subsumes a patch, drop it on the next sync (we already did
this once — see "Dropped" below).

To see the live carry-list against upstream:

```
git fetch origin            # bytedance/deer-flow
git log --oneline origin/main..argus
git diff --stat origin/main..argus
```

Current pin: see `Argus/VERSIONS.md` (the `DeerFlow @ <sha>` row). As of
2026-05-29 the fork sits at 10 functional patches (#1–#10); ~60% of the diff
line count is tests.

---

## Carried patches

### 1. `aio_sandbox`: prepend `set +H;` to disable bash history expansion
- **File:** `backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox.py` (+ test)
- **Why:** Commands that pipe HTML/JS containing `!DOCTYPE`, `!gl`, or other
  `!`-prefixed tokens through the sandbox shell blow up with "event not
  found" because bash history expansion is on. `set +H;` disables it; no
  other side effects.
- **Conflict risk:** Low. Two `exec_command` call sites; conflicts only if
  upstream changes those lines (it did once — 2-min resolution to keep both
  `wrapped_command` and the new `no_change_timeout` param).
- **Delete-when:** upstream disables history expansion in the sandbox shell,
  or the aio-sandbox provider exposes a shell-flags hook.
- **PR-candidate:** yes — small, defensible, generally useful.

### 2. `sandbox_audit`: raise `_MAX_COMMAND_LENGTH` 10_000 → 131_072
- **File:** `backend/packages/harness/deerflow/agents/middlewares/sandbox_audit_middleware.py` (+ test)
- **Why:** The 10k default truncates legitimate large commands (e.g. piping a
  big file through a one-liner). Qwen on Argus produces these. 131072 matches
  the model's context-window-ish ceiling.
- **Conflict risk:** None observed (upstream hasn't touched the constant).
- **Delete-when:** upstream makes this configurable via env/config.
- **PR-candidate:** maybe — better as a config knob upstream than a constant bump.

### 3. `loop_detection`: shrink read_file bucket 200 → 50 lines
- **File:** `backend/packages/harness/deerflow/agents/middlewares/loop_detection_middleware.py` (+ test)
- **Why:** Tuning for Qwen's behavior — it re-reads large files in a loop more
  readily than the models upstream tuned against; a smaller bucket trips the
  loop detector sooner.
- **Conflict risk:** Medium. Upstream rewrote this subsystem once already (see
  "Dropped" — three sibling patches were retired). This one survived as a
  one-line constant. Watch it on every sync.
- **Delete-when:** upstream's per-tool frequency overrides (PR #2711, now
  merged) may make this expressible as config — evaluate moving it there.
- **PR-candidate:** no (Qwen-specific tuning).

### 4. `checkpointer`: add `AsyncPostgresSaver.aprune`
- **Files:** `backend/packages/harness/deerflow/runtime/checkpointer/_postgres_aprune.py` (new),
  `.../async_provider.py` (4-line import) (+ test)
- **Why:** The langgraph postgres checkpointer ships `adelete_thread` but no
  `aprune`; langgraph_api warns about unbounded checkpoint growth. This adds a
  monkey-patched `aprune` (keep-latest + delete-all strategies). Implemented as
  an import-activated monkey-patch so it's idempotent and self-disabling.
- **Conflict risk:** Low. New file + a 4-line import block in async_provider.
  (The 2026-05-28 upgrade moved the checkpointer dir `agents/` → `runtime/`;
  we relocated the patch accordingly. Watch for further dir moves.)
- **Delete-when:** `langgraph-checkpoint-postgres` ships a native `aprune`.
  The patch is a no-op if that happens (it checks before installing).
- **PR-candidate:** yes — ideally upstreamed into
  `langgraph-checkpoint-postgres` rather than deer-flow.

### 5. `models/factory`: per-event-loop httpx client for ChatOpenAI
- **File:** `backend/packages/harness/deerflow/models/factory.py` (+ test)
- **Why:** langchain-openai caches the async httpx client process-globally;
  LangGraph's worker spins a fresh event loop per task, and reusing a client
  from a torn-down loop raises "Event loop is closed" at stream cleanup. We
  bypass the broken cache with a per-loop client (WeakValueDictionary).
- **Conflict risk:** **Medium-high.** This is the most substantive logic patch
  (~60 lines) and lives in a file upstream actively refactors (model
  construction). Most likely to break subtly on a big upgrade. The test guards
  the behavior — run it after every rebase.
- **Delete-when:** langchain-ai/langchain#35783 (the upstream cache bug) is
  fixed, OR DeerFlow stops sharing httpx clients across loops.
- **PR-candidate:** the real fix belongs in langchain-openai, not here.

### 6. `lead_agent/prompt`: add `<file_editing>` guidance block
- **File:** `backend/packages/harness/deerflow/agents/lead_agent/prompt.py` (+ test)
- **Why:** Without explicit edit-vs-rewrite guidance, Qwen defaults to bash
  heredocs for every file write — including fixes to files it just wrote —
  roughly doubling wall-time on iterative coding tasks.
- **Conflict risk:** **Medium-high.** `prompt.py` is the single most
  upstream-churned file we touch (8 upstream commits last sync). Expect to
  re-place this block manually on most upgrades; the test asserts it lands
  inside `<working_directory>`.
- **Delete-when:** never cleanly — this is Argus-specific prompt tuning. Could
  move to a SOUL.md / agent-config layer to stop patching prompt.py directly
  (see "Reduce-surface candidates" below).
- **PR-candidate:** no (Qwen-specific).

### 7. `lead_agent/prompt`: add `<debugging_when_stuck>` block
- **File:** `backend/packages/harness/deerflow/agents/lead_agent/prompt.py` (+ test)
- Same file, risk, and delete-when as #6. Two prompt blocks, one file.

### 8. `lead_agent/todo_middleware`: ArgusTodoMiddleware variant
- **Files:** `backend/packages/harness/deerflow/agents/middlewares/argus_todo_middleware.py` (new),
  `.../lead_agent/agent.py` (wiring) (+ test)
- **Why:** A planner-aligned TodoMiddleware variant for qwen-local-coder
  (Argus iteration 6.3) — keeps `state.todos[]` in sync with the planner's
  plan.json so the UI progress signal is accurate.
- **Conflict risk:** Low for the new file; Medium for the `agent.py` wiring
  (26 lines into a file upstream may refactor).
- **Delete-when:** upstream's TodoMiddleware gains planner-alignment, or we
  move to upstream's default.
- **PR-candidate:** maybe (generalize first).

### 10. `telegram`: HTML formatting + animated emoji indicator + channel-aware artifact presenter
- **Files:** `backend/app/channels/_telegram_format.py` (new),
  `backend/app/channels/_artifact_presenter.py` (new),
  `backend/app/channels/telegram.py` (patch `send()`/`_send_one()`,
  `_send_running_reply()`, `_clear_working()`, `_working_msg` state),
  `backend/app/channels/manager.py` (4-line `_prepare_artifact_delivery` hook +
  2 call sites) (+ tests `test_telegram_format.py`, `test_telegram_send.py`,
  `test_artifact_presenter.py`)
- **Why:** Three real-use problems with the bundled Telegram channel:
  1. **Formatting.** Upstream sends raw markdown with no `parse_mode`, so
     Telegram renders `**bold**`/backticks literally. We convert agent markdown
     → Telegram-native HTML (`parse_mode=HTML`) and chunk on the 4096 ceiling,
     with a plain-text fallback if Telegram rejects the HTML. Formatter ported
     from the ateam bot (`TOOLS/ateam-bot/formatting.py`).
  2. **Working indicator → live stage emoji.** Upstream sends a literal
     "Working on it..." text reply that's never removed. We instead show an
     animated lone emoji reflecting the agent's current execution stage,
     derived from the langgraph stream by the manager (`_stage_from_chunk`):
     👀 received → 🧠 thinking → 📝 planning (write_todos/todos) → 🔍 searching
     (search tool) → 🔧 working (other tool). Telegram flips to the streaming
     path (`CHANNEL_CAPABILITIES["telegram"].supports_streaming=True`) to see
     stages, but does NOT receive streamed partial answer text (suppressed in
     `_handle_streaming_chat` for telegram only). Because Telegram animates a
     lone emoji only on first SEND (never on edit — core.telegram.org/api/
     animated-emojis), each stage change DELETES the old emoji message and
     SENDS a new one; re-sends are throttled to `stage_min_interval` (~6s, the
     animation length) and skip rapid intermediate stages (latest-wins).
     Whichever emoji is showing is deleted when `is_final` arrives. Reaction
     fallback if the initial send fails. Config:
     `channels.telegram.{stage_emoji (map), stage_min_interval}`. (Supersedes
     the earlier `working_emoji_2`/`working_emoji_delay` edit-based two-stage
     model, which couldn't re-animate.)
  3. **Artifact presentation.** For Telegram, `present_files` HTML reports are
     turned into VIEWABLE links to the per-stack `/f/` fileserver (nginx) by a
     channel-aware presenter, instead of force-downloaded. Hooked in the
     manager (the seam that knows the target channel); other channels
     unchanged. Also auto-presents *orphan* artifacts — files the agent wrote
     to the outputs dir during the run but didn't `present_files` (it pasted
     the source into chat instead, observed with SVG) — detected by mtime, but
     SCOPED to viewable end-products only (`_ORPHAN_PRESENT_EXTS`:
     .html/.svg/.pdf/.png/.jpg/.gif/.webp). Scratch the agent leaves in
     outputs/ (a `.py` weather-fetcher, `.json`/`.csv`/`.txt`/`.log`) is never
     auto-linked — that would spam a "/f/fetch.py" link onto a plain answer.
     Explicit `present_files` still presents anything. Once a file is linked,
     the redundant >600-char inline code dump is stripped from the chat text.
     The chunker is tag-aware: it never splits inside a `<pre>`/`<code>`/
     `<blockquote>`, and splits an oversized block into several valid same-kind
     blocks, so a big report dump can't produce unclosed-tag HTML.
  4. **Split-paste coalescing.** Telegram chunks a long paste into several
     messages sent within ~1s; the manager's dispatch loop spawns a task per
     message, so same-thread messages raced and the 2nd+ hit a 409 "thread
     busy" (runs use `multitask_strategy="reject"`; the runtime doesn't
     implement `enqueue`) and were LOST. A `MessageCoalescer`
     (`app/channels/_coalesce.py`) debounces CHAT messages per
     (channel, chat, topic) for `coalesce_window` (default 2.5s) and dispatches
     the burst as ONE combined turn. Commands bypass it; `coalesce_window<=0`
     disables it. Fixes the lost-message bug and incidentally serializes
     same-conversation turns.
- **Conflict risk:** **Medium.** `telegram.py` and `manager.py` are in the
  2.0-rc channels subsystem upstream still iterates on. The manager hook is
  deliberately tiny (one helper + channel check) to keep the merge surface
  small; the two new modules are conflict-free (new files). Watch `send()` and
  `_prepare_artifact_delivery` on each sync.
- **Delete-when:** upstream gives the Telegram channel HTML/markdown rendering,
  a removable working-indicator hook, and a per-channel artifact-presentation
  hook. Until then this is Argus product behavior, not a bug workaround.
- **PR-candidate:** partial — the HTML formatter + an indicator hook could be
  upstreamed; the `/f/`-link presenter is Argus-specific (depends on our
  per-stack fileserver) and stays local.

### 11. `pythia_retrieval_middleware`: deterministic company-KB retrieval before the model call

- **What:** A `before_model` middleware (gated by `PYTHIA_ROUTER_INJECT`; legacy
  alias `PYTHIA_RETRIEVAL_ENABLED`) that, on the first model call of a turn,
  asks kb-api's router what company-knowledge context to fetch and injects the
  cited results as a `HumanMessage`, so a non-thinking Qwen lead agent answers
  from authoritative context in ONE shot instead of (unreliably) emitting a
  `pythia_query` MCP tool call or confabulating "no access".
- **Thin client (2026-06-02 rewrite):** routing + fetching now live server-side
  in kb-api `POST /{project}/answer` (router.py: entity vs chunk vs recency vs
  current_record vs none; 97% routing accuracy on the live golden set, zero new
  GPU). The middleware no longer classifies or calls pythia_query itself — it
  makes one `/answer` call and injects whatever `context_blocks` return; empty
  result (Route.NONE / off-topic / kb-api error) injects nothing and the agent
  falls back to its MCP tools. All routing logic + tests are in kb-api, shared
  with the Slack @pythia bot. (Superseded the original patch #11, which embedded
  its own company-vs-other embedding classifier + a pythia_query call here.)
- **Non-persisting injection (2026-06-07):** switched from `before_model`
  (which returns a STATE UPDATE — the injected `HumanMessage` was committed to
  thread history and rendered in the WebUI/Slack/Telegram/exports) to
  `wrap_model_call` / `awrap_model_call` + `ModelRequest.override(messages=...)`.
  `override()` is immutable, so the KB context reaches the model for THAT call
  only and is NEVER persisted to thread state. It is therefore invisible on every
  surface by construction (no per-channel suppression, no frontend patch).
  Verified live: thread checkpoints contain the user msg + AI answer but zero
  `[pythia-kb-context]` blobs. Note: `wrap_model_call` fires on EVERY model call
  in the loop (not once like `before_model`), so the once-per-turn guard moved to
  message inspection (skip if an `AIMessage` already follows the latest
  `HumanMessage`).
- **Conflict risk:** Low. One file; `wrap_model_call`/`override` are stable
  langchain 1.x agent-middleware APIs. Injects a `HumanMessage` into the request
  (system-msg-not-at-start 400 fix).
- **Delete-when:** if DeerFlow gains a first-class pre-model retrieval hook that
  can call an external router. Until then this is Argus product behavior.

### 12. `sandbox`: detect Created-but-not-Running (rootless port-bind race)

- **What:** On per-thread sandbox start, treat a container stuck in `Created`
  (never reaching `Running`) as a failure and surface it, instead of hanging
  the turn. Root cause was a rootless-podman port-bind race, not the model.
- **Conflict risk:** Low. Localized to the sandbox start path.
- **Delete-when:** upstream sandbox start grows its own Created-state timeout.

### 13. `uploads_middleware`: steer uploaded images to `view_image`

- **What:** In the `<uploaded_files>` context block, detect image uploads
  (`.jpg/.jpeg/.png/.webp`, mirroring `view_image_tool.py`s allowlist) and
  emit `view_image(image_path=...)` guidance instead of the document
  read_file/grep/glob workflow.
- **Why:** The frontend uploads images by path (`additional_kwargs.files`)
  and never inlines them as `image_url` blocks, so `view_image` is the ONLY
  route to the pixels. The doc-oriented guidance is useless for an image and
  left a vision-capable Qwen with no way to see an uploaded screenshot. The
  capability was wired (supports_vision + tool + ViewImageMiddleware) but the
  model was never told to call the tool, so image understanding never worked
  end-to-end through the chat UI.
- **Conflict risk:** Low. One early-return branch in `_format_file_entry`;
  non-image entries are byte-for-byte unchanged. Test in
  `tests/test_uploads_middleware_core_logic.py::TestImageFileGuidance`.
- **Delete-when:** the frontend inlines image uploads as `image_url` content
  blocks (then the model sees them directly, no tool round-trip), OR upstream
  adds image-aware guidance to the uploads block.

### 14. `gateway/routers/channels`: proactive notify endpoint

- **What:** `POST /api/channels/{name}/notify` — injects a synthetic
  `InboundMessage` (chat_id, text, optional user_id/topic_id) onto the
  channel MessageBus. Guarded by the internal service token specifically
  (`X-DeerFlow-Internal-Token`); an SSO session is NOT accepted because the
  caller chooses chat_id/user_id (identity impersonation otherwise).
- **Why:** Scheduled jobs (Atlas morning briefing, Chronos-fired turns) need
  to start a turn that is DELIVERED to the citizen's IM chat. The channel
  pipeline already does everything (thread mapping via the store, agent run,
  HTML formatting, artifact delivery, reply threading) but only fires on
  polled inbound messages; this is the missing trigger surface. With
  `topic_id=None` a Telegram private chat reuses its persistent thread, so
  the citizen can reply to the briefing naturally.
- **Conflict risk:** Low. Additive endpoint at the end of a small router
  upstream rarely touches; one import widened (`Request`). Tests in
  `tests/test_channel_notify.py`.
- **Delete-when:** upstream grows a proactive/outbound message API for
  channels (watch `app/channels/` for a send-without-inbound surface).

### 15. `gateway` + `frontend`: trust the Caddy SSO identity (drop the second login)

- **What:** Let the gateway authenticate a request off the edge-verified
  `X-Auth-Email` header in place of DeerFlow's own email+password login, so a
  citizen who already passed Google SSO at the Caddy edge is not asked to log
  in again.
  - `app/gateway/sso_auth.py` (new): `trusted_sso_email(headers)` returns the
    email ONLY when the request also carries a matching `X-Auth-Proxy-Secret`
    (constant-time compare vs `DEER_FLOW_SSO_PROXY_SECRET`). Fail-closed: no
    secret configured → SSO trust disabled.
  - `app/gateway/auth_middleware.py`: no cookie + trusted SSO email →
    resolve/auto-provision the user by email, stamp `request.state.user`
    (parallel to the `X-DeerFlow-Internal-Token` branch).
  - `app/gateway/deps.py`: `resolve_or_provision_sso_user(email)`.
  - `app/gateway/routers/auth.py`: `/me` prefers `request.state.user` (set by
    the middleware) before the cookie resolver — and guards against the
    email-less synthetic internal user.
  - `frontend/src/core/auth/server.ts`: the SSR auth check
    (`getServerSideUser`) now reads the incoming request's SSO headers and
    forwards them to the gateway `/me`; without this the Next.js server-side
    fetch only sent the cookie, so a cookieless SSO browser still SSR-redirected
    to `/login`.
- **Why:** Caddy already proves identity (Google SSO via oauth2-proxy, injects
  a spoof-protected `X-Auth-Email`). The second DeerFlow login was pure
  friction and split the user identity. Trusting the edge identity also unifies
  the `user_email` the PythiaRetrievalMiddleware uses to mint per-person ring
  caller tokens.
- **Security:** the gateway is reachable on the tailnet bypassing Caddy, so the
  proxy-secret gate is what makes the header trustworthy — a direct caller
  lacks the secret and its `X-Auth-Email` is ignored (falls back to
  cookie/login). Caddy injects `X-Auth-Proxy-Secret` on the DeerFlow stacks and
  strips any client-supplied copy. Mirrors the Lexis trusted-proxy pattern.
- **Conflict risk:** Medium. Touches `auth_middleware.py` and `auth.py` (auth
  hot path) and one frontend SSR file; new `sso_auth.py` is additive.
- **Delete-when:** upstream gains a first-class "trust an upstream-authenticated
  identity header" / reverse-proxy-auth mode for both the gateway and the SSR
  auth check.

### 16. `gateway/csrf_middleware`: mint the CSRF cookie for trusted-SSO sessions

- **What:** Issue the `csrf_token` cookie to a citizen whose session is
  established via the patch #15 trusted-SSO path, not only on a local-login
  POST. The Double Submit Cookie design mints `csrf_token` solely in the
  `is_auth_endpoint and POST` branch (login/register/initialize). An SSO
  citizen never POSTs those endpoints, so they got no cookie, and the frontend
  (`fetcher.ts` / `api-client.ts`) had nothing to echo in `X-CSRF-Token`. Their
  first state-changing request (e.g. sending a query -> POST /api/threads,
  /api/langgraph/*) was then rejected by the very CSRF check this fork relies
  on, with 403 "CSRF token missing. Include X-CSRF-Token header.".
  - `app/gateway/csrf_middleware.py`: compute `_sso_first_contact` =
    `trusted_sso_email(headers) is not None and no csrf_token cookie`. When
    true, (a) skip the double-submit rejection for THAT request only (genuine
    first contact, symmetric with how auth-endpoint POSTs are exempt), and
    (b) mint + `set_cookie(csrf_token, ...)` on the response. Subsequent
    requests carry the cookie and take the normal double-submit path unchanged.
- **Why:** patches #15 (SSO trust) and the pre-existing CSRF double-submit
  design were never reconciled: #15 removed the only event that minted the
  cookie. This is the missing half of #15 - without it, SSO citizens can load
  the UI but every query 403s.
- **Security:** the relaxation is gated by `trusted_sso_email`, the same
  proxy-secret (`DEER_FLOW_SSO_PROXY_SECRET`, constant-time) gate as #15, so a
  direct tailnet caller cannot use it to bypass CSRF - without the secret
  `_sso_first_contact` is always False and the standard double-submit check
  applies. The minted cookie keeps the existing attributes
  (`httponly=False` for JS read, `secure` when https, `samesite=strict`).
- **Conflict risk:** Low. One method (`CSRFMiddleware.dispatch`) plus one
  import; additive to the existing minting branch.
- **Delete-when:** patch #15 is dropped (upstream reverse-proxy-auth mode), or
  upstream's CSRF middleware learns to mint the token for proxy-authenticated
  sessions.

### 17. `config/agents_config`: fall back to the shared agent dir when the per-user dir has no config.yaml

- **What:** `resolve_agent_dir` preferred the per-user layout
  (`users/<uid>/agents/<name>/`) whenever that directory merely *existed*. But
  DeerFlow writes per-agent `memory.json` into that per-user dir even for agents
  whose *definition* (config.yaml + SOUL.md) lives only in the shared layout
  (`agents/<name>/`). So a per-user dir holding nothing but `memory.json`
  shadowed the shared `config.yaml`, and `load_agent_config` then raised
  `FileNotFoundError: Agent config not found`. Symptoms: selecting
  `pythia-internal` / `pythia-ext` in the web UI (per-user memory dirs exist for
  the SSO user), and routing a channel turn to a shared agent (Telegram/briefing
  run with effective `user_id=default`, whose `agents/atlas/` orphan dir blocked
  `atlas`).
  - `backend/packages/harness/deerflow/config/agents_config.py`
    (`resolve_agent_dir`): prefer the per-user dir only if `user_path /
    "config.yaml"` exists; otherwise fall back to the shared dir. The per-user
    `memory.json` is still loaded independently by MemoryMiddleware, so no
    memory is lost.
- **Why:** the opt-in agent framework (Atlas / Pythia-Internal / Pythia-External
  / Plain as first-class selectable agents) requires every shared agent to
  resolve regardless of which orphan per-user memory dirs happen to exist. The
  prior data-layer workaround (co-locating config.yaml into each per-user dir)
  did not scale to new citizens.
- **Conflict risk:** Low. One condition in one function; additive.
- **Delete-when:** upstream makes per-user agent dirs config-complete (writes a
  config.yaml alongside memory.json), or unifies the two layouts.

### 18. `agents_config` + `lead_agent/factory`: `uses_planner_pipeline` flag

- **Files:** `config/agents_config.py`, `agents/lead_agent/agent.py`,
  `tests/test_argus_todo_middleware.py`
- **What:** Adds a `uses_planner_pipeline: bool = False` field to
  `AgentConfig`. The `_create_todo_list_middleware` factory now checks
  `agent_config.uses_planner_pipeline` instead of matching on
  `agent_name == "qwen-local-coder"`. Any agent that sets the flag gets
  `ArgusTodoMiddleware` (planner-aligned prompt); others get the upstream
  `TodoMiddleware`.
- **Why:** The glm-planner agent (GLM-5.2 lead + local-qwen subagent) needs
  the same ArgusTodoMiddleware as qwen-local-coder, but the name-based match
  would not cover it. The flag generalizes the routing so any agent using the
  planner/critic skill pipeline gets the right middleware.
- **Delete-when:** upstream DeerFlow gains a first-class "planner pipeline"
  agent type or makes the middleware selection configurable per-agent.

### Infra-only (not a code patch)
- `.github/workflows/argus-ci.yml` — Argus-only test workflow. Runs the patch
  tests. New file, zero conflict risk.
- `fix(lint)` commit — ruff cleanup after an upstream merge. Folds away on the
  next squash; not a standalone concern.

### 19. `frontend/agent chat page`: drive the input context from the agent's pinned model

- **File:** `frontend/src/app/workspace/agents/[agent_name]/chats/[thread_id]/page.tsx`
- **What:** When rendering an agent chat, set the input `context.model_name`
  to `getThreadModelName(threadId) ?? agent?.model ?? undefined` (both the
  `useThreadStream` context and the `<InputBox>` prop). Precedence:
  THIS thread's explicit model override wins; otherwise the agent's pinned
  model is the default. Deliberately does NOT fall back to the global
  last-picked model, so switching agents applies the new agent's default
  (glm-planner -> GLM, atlas -> Qwen) while a per-thread pick still sticks
  for that thread. Requires exporting `getThreadModelName` from the
  `core/settings` barrel. (Two earlier cuts same day: `agent?.model ??
  settings...` made the pin always win over the picker; `settings.context
  .model_name ?? agent...` made the global last-pick stick across agents.
  This per-thread-override-else-agent-default is the intended behavior.)
- **Why:** The InputBox derives Flash/Reasoning/Pro/Ultra mode gating from
  `selectedModel.supports_thinking`, where `selectedModel` resolves from
  `context.model_name`. The agent page only injected `agent_name`, never the
  agent's pinned `model`, so for a thinking-capable agent (e.g. glm-planner ->
  glm-nw) the UI resolved the stale global model and greyed out every mode but
  Flash, even though the agent ran on glm-nw server-side. Sourcing model_name
  from the agent makes the UI gating match the model the agent actually uses.
- **Delete-when:** upstream threads the agent's pinned model into the chat
  input context, or the mode gating reads the agent model directly.

### 16. `agents_config` + `lead_agent/factory`: `uses_planner_pipeline` flag

- **Files:** `config/agents_config.py`, `agents/lead_agent/agent.py`,
  `tests/test_argus_todo_middleware.py`
- **What:** Adds a `uses_planner_pipeline: bool = False` field to
  `AgentConfig`. The `_create_todo_list_middleware` factory now checks
  `agent_config.uses_planner_pipeline` instead of matching on
  `agent_name == "qwen-local-coder"`. Any agent that sets the flag gets
  `ArgusTodoMiddleware` (planner-aligned prompt); others get the upstream
  `TodoMiddleware`.
- **Why:** The glm-planner agent (GLM-5.2 lead + local-qwen subagent) needs
  the same ArgusTodoMiddleware as qwen-local-coder, but the name-based match
  would not cover it. The flag generalizes the routing so any agent using the
  planner/critic skill pipeline gets the right middleware.
- **Delete-when:** upstream DeerFlow gains a first-class "planner pipeline"
  agent type or makes the middleware selection configurable per-agent.

---

### 20. `ViewImageMiddleware` + `lead_agent/factory`: vision-describe for non-vision leads

- **Files:** `backend/packages/harness/deerflow/agents/middlewares/view_image_middleware.py`,
  `backend/packages/harness/deerflow/agents/lead_agent/agent.py`
- **What:** When the lead model is NOT vision-capable, `view_image` results
  used to be dropped (the factory only attached `ViewImageMiddleware` for vision
  leads). Now the middleware is also attached for non-vision leads with a
  `vision_model_name` (auto-discovered: the first `supports_vision` model in
  config, i.e. `local-qwen`). In that mode `abefore_model` routes each viewed
  image through the vision model for a render-verification-focused TEXT
  description (layout, verbatim text, colors, visible rendering defects) and
  injects that text instead of the raw image. Vision leads are unchanged
  (direct image inject). The sync `before_model` defers to `abefore_model` in
  describe-mode (cannot await); the lead-agent loop uses the async path.
- **Why:** `glm-planner` runs on `glm-nw` (GLM-5.2, non-vision), but its
  `render-and-verify` skill calls `view_image` and tells the agent to inspect
  the screenshot. With no vision routing, the visual half of the loop was
  silently dead. This lets a non-vision planner lead still see (via Qwen's
  description) what it rendered.
- **Delete-when:** upstream adds vision-model routing for non-vision leads, or
  all lead models are vision-capable.

## Dropped patches (history — do not re-add)

- **#9 `langgraph_auth` lazy-init** (plus the `--allow-blocking` deploy flag) -
  retired 2026-06-03 when Argus stopped running the standalone `langgraph dev`
  server. Every project's nginx now routes `/api/langgraph/*` to the gateway
  runtime (`app.gateway.app`), whose lifespan already runs
  `init_engine_from_config()` and whose `auth_middleware` sets the user
  contextvar. `langgraph_auth.py` only executed under `langgraph dev`, so the
  patch was dead code; reverted to upstream. The standalone server also had a
  user-identity bug (every run resolved user=default, breaking
  uploads/sandbox/view_image), independently fixed by the gateway-runtime move.
  Do not revive unless we go back to running `langgraph dev` standalone.

- **3 loop-detector patches** (nudge-toward-observation, edit-aware-reset +
  thresholds 5/8, drop layer-2 frequency detection) — retired during the
  2026-05-28 upgrade because upstream rewrote the loop detector with a
  warning-queue architecture (`_pending_warnings`) that subsumes their intent.
  This is the fork working as intended: a patch became unnecessary and we let
  it go. If loop behavior regresses on Qwen, re-tune against the *new*
  architecture rather than reviving the old patches.

---

## Reduce-surface candidates (future work)

The cheapest fork is the smallest one. Candidates to shrink the patch surface:

- **Prompt blocks (#6, #7):** move `<file_editing>` and `<debugging_when_stuck>`
  out of `prompt.py` and into a SOUL.md / agent-config layer if DeerFlow
  supports prompt-section injection. Removes the two highest-churn-risk
  patches from the most-churned file.
- **Constants (#2, #3):** push upstream to make `_MAX_COMMAND_LENGTH` and the
  loop-detector buckets configurable; then carry config, not code.
- **aprune (#4):** upstream PR candidate. A real gap, not tuning.

---

## Upgrade routine

1. `git fetch origin && git log --oneline argus..origin/main` — see what's new.
2. Audit for breaking changes, new auth/channel/skill APIs, dir moves.
3. `git checkout -b argus-rebase-<date> argus && git rebase origin/main`.
4. Resolve conflicts (prompt.py and factory.py are the usual suspects).
5. Run the patch tests (`backend/tests/test_*` for the touched areas).
6. Bump `Argus/VERSIONS.md` SHA; `make deerflow-clone && make deerflow-rebuild`.
7. `make smoke`; canary on research/knowledge before atlas-* stacks.
8. Force-push `argus`; tag `argus-deployed-<date>`.
9. Update this file: drop subsumed patches, note new ones, update delete-when.
