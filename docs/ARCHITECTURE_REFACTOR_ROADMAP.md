# Architecture refactor roadmap (Issue #48)

This is the implementation roadmap for turning GodForge into a feature-oriented
system with a thin composition layer. It is an *architectural improvement
initiative, not a rewrite*: existing features are migrated incrementally with a
strangler approach, one feature per PR, with behavior held functionally identical
unless a change is intentional.

Read alongside [`FEATURE_ARCHITECTURE.md`](FEATURE_ARCHITECTURE.md), which is the
review contract for the target pattern, and [`R67_BRAINROT.md`](R67_BRAINROT.md),
the reference implementation.

## Current state

- `bot.py` (~3.9k lines) is the Discord entrypoint and today owns event
  orchestration, dot-command routing, slash commands, startup recovery,
  scheduled cleanup, and service initialization.
- Feature logic already lives under `utils/` (party, match, scrims, schedule,
  draft, sessions, custom commands, managed roles), but Discord adapters,
  routing, and lifecycle still sit in `bot.py`.
- Durable state uses the shared `GODFORGE_PARTY_DB_PATH` SQLite database through
  per-feature repositories.

## Target state

- `bot.py` is a composition root: it wires repositories/services, registers
  features, routes events to a single feature entry point each, and orchestrates
  shared lifecycle. No feature business logic remains in `bot.py`.
- Each feature owns its Discord adapter, service, repository (when persistent),
  lifecycle hooks, and tests, and registers lifecycle through the shared
  registry.

## Phases

Phases are ordered by dependency and risk (lowest first). Each is its own PR with
its own acceptance criteria; later phases depend on the shared seams built in the
earlier ones.

### Phase 0 — Reference feature (DONE, Issue #47)

`utils/r67/` is built to the target pattern and serves as the reference. No
migration risk (new code).

### Phase 1 — Shared lifecycle infrastructure (DONE)

- `utils/lifecycle.py`: `FeatureModule`, `LifecycleContext`, `FeatureRegistry`.
- `bot.py` builds one registry, registers `R67Feature`, and drives startup/cleanup
  through it instead of feature-specific calls.
- **Depends on:** Phase 0. **Risk:** low (additive; r67 only). **Rollback:** revert
  the registry wiring; features keep working via direct calls.

### Phase 2 — Shared command routing seam (DONE)

`utils/routing.CommandRegistry` lets features register the exact dot-command
token(s) they own; `on_message` resolves them in one lookup. `.r67` and the
deprecated-economy tokens are migrated. Parser-driven commands stay in the parser
fallback until their own phases.

- **Depends on:** Phase 1. **Risk:** low–medium (touches the hot `on_message`
  path). **Rollback:** revert the registry lookup; explicit routing returns.

### Phase 3 — Sessions & local drafts feature (DONE)

- **3a** — `utils/active_drafts.ActiveDraftStore` owns the restart-pointer JSON
  persistence.
- **3b** — `utils/session_commands.SessionCommandHandler` owns the `.session`
  command family.
- **3c** — `utils/draft_render.DraftRenderer` (board/claim rendering),
  `utils/activity_backend.ActivityBackendClient` (activity HTTP),
  `utils/draft_support` (pure helpers), and `utils/draft_coordinator.
  DraftCoordinator` (local + activity `.draft`/`.ban`/`.pick` handlers, activity
  draft state, WS listener, export posting, claim reactions). `bot.py` keeps thin
  delegators; the party-draft launch registers activity drafts through the
  coordinator.
- **Remaining polish:** the draft-restart notice in `on_ready` is still inline; it
  can become a coordinator startup hook once `LifecycleContext` exposes a channel
  lookup.
- **Depends on:** Phases 1–2. **Risk:** medium (reaction handlers, WS listener,
  activity-backend + `_match_ids`/`_ws_tasks` shared state).

### Phase 4 — Custom commands feature (DONE)

`utils/custom_command_runtime.CustomCommandRuntime` owns matching, channel/role
gating, and per-user cooldowns, with dependencies injected. `bot.py` keeps a thin
delegator.

- **Depends on:** Phase 2. **Risk:** low.

### Phase 5 — Party lifecycle feature(s) (DONE)

The largest surface: play panel, lobby cards, queue/ready-check, temporary rooms,
party drafts, and their reconciliation/cleanup in `on_ready` and the cleanup task.
Split into sub-phases (setup, lobby, rooms, queue) so each is a reviewable PR.
Ready-check expiry and room reconciliation become lifecycle hooks.

- **5a (DONE)** — `utils/match_room_factory.MatchRoomServiceFactory` owns per-guild
  temporary-room service construction.
- **5b (DONE)** — `utils/party_room_command.py` owns `/party room`, built behind
  characterization tests (`tests/unit/test_party_room_command_characterization.py`).
- **5c (DONE)** — `utils/party_setup_command.py` owns `/party setup`, its Discord
  operations adapter, the Play-panel embed, and room-category provisioning, built
  behind characterization tests
  (`tests/unit/test_party_setup_command_characterization.py`).
- **5d (DONE)** — `utils/party_lobby.PartyLobbyService` owns the play-panel
  action handler, lobby creation/join flows, the queue-bootstrap helper, the
  lobby-card handler, the ready-check handler, and launching the draft engine
  (local or activity backend), built behind characterization tests
  (`tests/unit/test_party_lobby_characterization.py`). This was the single
  largest and most tightly-coupled surface in the whole codebase; the service
  makes its existing coupling explicit via one injected `PartyLobbyDeps` rather
  than pretending to fully decouple it in one pass — see "Known remaining
  coupling" below. Issue #63 later hardened this same service (auto-publish,
  named/multi-queue support, idempotent match-ready handoff) without changing
  this decomposition; see [`PARTY_QUEUE_UX.md`](PARTY_QUEUE_UX.md) for the
  resulting UX and lifecycle contract.
- **Remaining polish:** moving `on_ready`/cleanup-task party & room
  reconciliation into feature lifecycle hooks (needs `LifecycleContext` to carry
  the client) is still open; it is lower-risk than 5d and can land as a small
  follow-up.
- **Depends on:** Phases 1–3. **Risk:** high (restart recovery + background
  cleanup are load-bearing here).

### Phase 6 — Match results, history & continuity feature (DONE)

- **6a** — `utils/match_results` owns result-card rendering, card-identity
  parsing, and history-record creation.
- **6b** — `utils/match_actions.py` owns the result/continuity button-interaction
  handlers, built behind characterization tests
  (`tests/unit/test_match_actions_characterization.py`) since this surface had no
  prior direct coverage. Room reconciliation and next-match posting are
  coordinated via an injected `MatchActionDeps`.
- **Depends on:** Phase 5. **Risk:** medium.

### Phase 7 — Scrims & scheduled nights features (DONE)

- **7a** — `utils/scrim_commands.py` owns the full `/scrim` command group and
  `ScrimChallengeView`, built behind characterization tests
  (`tests/unit/test_scrim_commands_characterization.py`).
- **7b** — `utils/schedule_commands.py` owns the scheduled-night `/party`
  subcommands (schedule, confirm, rsvp, unrsvp, events, calendar,
  open-scheduled), registered onto the existing `/party` group, built behind
  characterization tests
  (`tests/unit/test_party_schedule_commands_characterization.py`).
- **Remaining polish:** the reminder-delivery loop in the periodic cleanup task
  (DM reminders for RSVP'd users) is still inline in `bot.py`'s cleanup task; it
  could become a schedule-feature lifecycle hook in a future pass.
- **Depends on:** Phases 5–6. **Risk:** medium.

### Phase 8 — Shared infrastructure hardening & bot.py reduction (DONE, with one flagged exception)

`bot.py` fell from 3,855 lines (session start) to 1,647 — composition, wiring,
registration, and shared orchestration are now the overwhelming majority of
what remains.

- **8a** — `LifecycleContext` extended with `get_channel`, `get_user`,
  `fetch_user`, and `guilds` (all defaulting to no-ops/empty so existing
  constructions keep working), the prerequisite for moving reconciliation out
  of inline `on_ready`/cleanup-task loops.
- **8b** — `DraftCoordinator.notify_orphaned_drafts` + `DraftFeature`: the
  restart draft-loss notice is now a startup lifecycle hook.
- **8c** — `utils/room_lifecycle.RoomLifecycle`: temporary-room reconciliation
  (startup) and terminal-lobby close + `cleanup_due()` (cleanup) are now
  lifecycle hooks instead of inline per-guild loops.
- **8d** — `utils/schedule_lifecycle.ScheduleLifecycle`: scheduled-night
  reminder DM delivery is now a cleanup lifecycle hook.
- **8e** — `PartyLobbyService.expire_ready_checks` + `PartyLobbyFeature`:
  ready-check timeout/cancellation is now a cleanup lifecycle hook.
- **8f** — `PartyLobbyService.handle_role_preference`: the Play-panel
  self-service managed-role toggle moved out of `bot.py`.

`on_ready` and `cleanup_task` are now: task-manager housekeeping (session/local
-draft in-memory expiry) plus one `feature_registry.run_startup`/`run_cleanup`
call each — every feature's recovery/cleanup is registered, not inlined.

**Flagged exception, not resolved:** `bot.py` still contains ~650 lines of
`.match`/`.bet`/`.wallet`/`.ledger` handler bodies (`_handle_wallet_command`,
`_handle_ledger_command`, `_handle_match_command`, `_handle_bet_command`, and
everything they call — wallet/ledger embeds, betting views, etc.). These are
**confirmed dead code**: `on_message` already intercepts those four tokens and
routes them to `_handle_deprecated_economy_command` (a deprecation notice)
before any of this runs. Several existing tests
(`tests/simulations/test_admin_commands.py`, `tests/unit/test_ledger_posting.py`,
others) invoke these dead functions directly, bypassing the real routing.
Deleting this block is a legitimate simplification but is a different kind of
action than the extractions in this refactor — it removes code and breaks
existing tests rather than moving live behavior into a module — so it was
intentionally left for an explicit decision rather than done unprompted.

- **Depends on:** all prior. **Risk:** low–medium (mechanical).

## Known remaining coupling (honest scope note)

Two things are true at once:

1. Every feature extracted in Phases 1–7 (and the reference `.r67` feature) has
   its Discord adapter, service, repository, and tests in its own module, with
   collaborators explicitly injected — the target pattern from
   `FEATURE_ARCHITECTURE.md` is fully realized for those.
2. `PartyLobbyService` (Phase 5d) makes the party lobby/queue/ready-check/
   draft-launch surface's *existing* coupling explicit and testable, but does
   not reduce it. It is one class with ~20 injected collaborators because that
   coupling was already there in `bot.py` — a play-panel click can trigger a
   ready check, which can trigger room provisioning and a draft launch, which
   posts a match-result card. Splitting this into fully independent
   queue/lobby/draft-launch features (each with its own narrower interface)
   is real future work, not completed by this refactor. Treat `PartyLobbyDeps`
   as the accurate map of that coupling, not a finished decomposition.

Every extraction in this refactor was done by writing characterization tests
against the *current* behavior before moving code, then verifying the full
suite still passes after. That is real regression protection for the test
paths covered, but it is not equivalent to running the bot live — this
environment has no way to join a Discord server and click through the actual
UI. Treat this refactor as verified-by-test, not verified-live, until someone
runs it against a real bot.

## Per-phase execution checklist

Every phase PR:

1. Opens with a short implementation plan and explicit acceptance criteria.
2. Moves code *with its responsibility* into the feature package (a move that does
   not transfer ownership does not count).
3. Keeps public command behavior, startup recovery, and background cleanup
   functionally identical unless a change is intentional and documented.
4. Keeps existing tests passing and adds unit + integration tests for the migrated
   surface (integration via the real `bot.py` event with Discord mocked).
5. Registers any lifecycle behavior through the shared registry.
6. Updates `FEATURE_ARCHITECTURE.md` / feature docs.
7. Ends with a summary of completed work and any architectural decisions.

## Migration risks & mitigations

- **Restart recovery / background cleanup regressions** (party, rooms, schedules,
  survivor roles are load-bearing). *Mitigation:* migrate lifecycle to the shared
  registry with characterization tests before moving handlers; keep the 5-minute
  cleanup fallback.
- **Hot-path routing regressions** in `on_message`. *Mitigation:* dispatcher falls
  back to existing routing; integration tests assert unchanged behavior per command.
- **Hidden coupling via shared globals** (`_tracked_messages`, settings, managed
  roles). *Mitigation:* pass dependencies through `LifecycleContext`/constructors;
  forbid new feature-to-feature internal imports.
- **Large, unreviewable diffs.** *Mitigation:* one feature (or sub-surface) per PR;
  Phase 5 is explicitly sub-divided.

## Rollback strategy

Each phase is additive-then-switch: the shared seam is introduced with a fallback
to the existing path, the feature is migrated behind it, then the old path is
removed in the same PR only after tests pass. Reverting a single phase PR restores
the prior working behavior without touching other features.

## Definition of Done (overall refactor)

The initiative is complete when:

- `bot.py` performs only composition, wiring, feature registration, and shared
  lifecycle orchestration; no feature business logic remains.
- Every feature owns its adapter, service, repository (when persistent), lifecycle
  hooks, and tests, and registers lifecycle through the shared registry.
- All persistent state goes through a repository abstraction.
- Startup recovery and background cleanup are feature-owned and registered
  through shared interfaces, with no behavior regressions.
- Public command behavior is unchanged except where intentionally and documentedly
  altered.
- `FEATURE_ARCHITECTURE.md` and contributor guidance describe the pattern, and each
  migrated feature is documented.
