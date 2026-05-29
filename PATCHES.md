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

### 9. `langgraph_auth`: lazy-init persistence engine for standalone `langgraph dev`
- **File:** `backend/app/gateway/langgraph_auth.py`
- **Why:** Upstream's `langgraph_auth.py` calls `get_local_provider()` on every
  authenticated request, but `init_engine_from_config()` runs only in the
  gateway's lifespan — never in the standalone `langgraph dev` server. Result:
  every authenticated `/api/langgraph/*` request 500s with "users table not
  initialized." We add a lazy `_ensure_engine_initialized()` (idempotent under
  an asyncio lock) in `@auth.authenticate`.
- **Companion (NOT a fork patch — deploy config):** the lazy init does
  synchronous filesystem work (os.getcwd, sqlite path resolution) that
  `langgraph dev`'s blockbuster watcher flags. Each per-project langgraph
  Quadlet runs `langgraph dev --allow-blocking`. That flag lives in the
  Quadlet Exec= line in the Argus infra repo, not here.
- **Conflict risk:** Medium. `langgraph_auth.py` is small and changes rarely,
  but it's gateway-auth code touched in the 2.0-rc wave.
- **Delete-when:** upstream runs `init_engine_from_config()` in the
  `langgraph dev` lifespan (the proper fix). At that point both this patch AND
  the `--allow-blocking` flag come off.
- **PR-candidate:** **yes — strongest candidate.** This is a genuine upstream
  bug in standalone-langgraph-dev mode, not Argus-specific tuning.

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
  2. **Working indicator.** Upstream sends a literal "Working on it..." text
     reply that's never removed. We instead send a standalone single-emoji
     message (Telegram animates lone emoji). It's a TWO-STAGE indicator:
     `working_emoji` (👀) for `working_emoji_delay` seconds, then edited in
     place to `working_emoji_2` (🧠). Stored per-chat; whichever emoji is
     showing is deleted when `is_final` arrives (and an answer before the
     delay cancels the swap). Reaction fallback if the initial send fails.
  3. **Artifact presentation.** For Telegram, `present_files` HTML reports are
     turned into VIEWABLE links to the per-stack `/f/` fileserver (nginx) by a
     channel-aware presenter, instead of force-downloaded. Hooked in the
     manager (the seam that knows the target channel); other channels unchanged.
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

### Infra-only (not a code patch)
- `.github/workflows/argus-ci.yml` — Argus-only test workflow. Runs the patch
  tests. New file, zero conflict risk.
- `fix(lint)` commit — ruff cleanup after an upstream merge. Folds away on the
  next squash; not a standalone concern.

---

## Dropped patches (history — do not re-add)

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
- **aprune (#4) and langgraph_auth (#9):** upstream PRs. Both are real gaps,
  not tuning — best path to zero.

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
