# GodForge Standalone Product Plan

Status: implementation complete; live validation pending (`v2.3.0-rc.3`)
Last updated: August 20, 2026

## Implementation tracker

All linked implementation issues are merged. This tracker remains as the
durable map from product intent to repository history. The Phase 1 party
lobby section below is kept for historical/directional context; for the
*current* Play panel workflow and lifecycle rules, see
[`PARTY_QUEUE_UX.md`](PARTY_QUEUE_UX.md), which Phase 1's queue-first
hardening pass superseded some details of.

- Foundation: [#18](https://github.com/diese-tech/lab-godforge/issues/18)
  durable lobby lifecycle and
  [#30](https://github.com/diese-tech/lab-godforge/issues/30) optional
  compatibility isolation.
- Zero-config onboarding:
  [#19](https://github.com/diese-tech/lab-godforge/issues/19) guild setup and
  [#20](https://github.com/diese-tech/lab-godforge/issues/20) managed cosmetic
  roles.
- Core lobby loop:
  [#21](https://github.com/diese-tech/lab-godforge/issues/21) through
  [#27](https://github.com/diese-tech/lab-godforge/issues/27).
- Game-night expansion:
  [#28](https://github.com/diese-tech/lab-godforge/issues/28) scheduling and
  [#29](https://github.com/diese-tech/lab-godforge/issues/29) team/scrim tools.
- Queue-first UX hardening:
  [#63](https://github.com/diese-tech/lab-godforge/issues/63) fixed a
  join/public-card desync bug and reworked the Play panel around a
  streamlined, multi-queue-safe workflow. See
  [`PARTY_QUEUE_UX.md`](PARTY_QUEUE_UX.md) for current behavior.

## Product direction

GodForge should become the SMITE 2 custom-night operating system for Discord:

> Form a ten-player lobby, confirm that everyone is ready, create fair
> role-complete teams, run a fearless draft, record the game-night outcome,
> and keep the group playing.

The core loop is:

```text
party formation
  -> ready check
  -> role-complete teams
  -> fearless draft
  -> result confirmation and history
  -> rematch or re-queue
```

GodForge must be a complete standalone product. ForgeLens is not part of the
required user journey. Its existing export and integration code should remain
available behind an optional, disabled-by-default adapter in case the companion
product returns, but GodForge workflows must work identically when ForgeLens is
absent.

## Product boundary

GodForge owns:

- Party formation and lobby discovery.
- Ready checks, waitlists, and substitute promotion.
- SMITE role preferences and role-complete team formation.
- Captains, team rooms, and fearless drafts.
- Game-night match state.
- Lightweight result confirmation and match history.
- Optional aggregate game-night statistics.
- Rematches, team shuffles, and re-queueing.

GodForge does not currently own:

- Economy, wallets, wagering, odds, payouts, or betting.
- Official SMITE ranks.
- Automated OCR or detailed per-player performance ingestion.
- A global player reputation score.
- A tournament bracket engine.
- Cross-server matchmaking before the single-server product proves demand.

ForgeLens compatibility is a failsafe integration surface only. No ForgeLens
failure may block lobby creation, drafting, result confirmation, history, or
rematches.

## Why this direction

Comparable Discord products validate several expectations:

- Modern LFG flows use buttons, forms, and browsable lobby cards rather than
  requiring users to memorize commands.
- NeatQueue validates a persistent queue that remains available across matches,
  a guided `/setup`, multiple team-selection modes, ready-up choices, temporary
  rooms, winner voting, rematches, and a safe test mode.
- Temporary text and voice rooms with automatic cleanup are table stakes.
- Ready checks, capacity limits, waitlists, and substitute promotion prevent
  no-shows from collapsing an event.
- MOBA communities need role-complete and reasonably fair teams, not only
  random splitting.
- Planned events and immediate LFG increasingly converge on the same live lobby
  workflow.
- Generic products usually stop at attendance or group formation. GodForge can
  differentiate by continuing through SMITE-specific drafting, results, and
  rematches.

The moat is not the number of bot commands. It is the shortest, most reliable
path from ten interested SMITE 2 players to a completed, recorded custom match.

## Target users

### Community PUG organizer

Turn ten interested members into two valid teams and begin drafting without
spreadsheets, repeated pings, or manually moving people.

### League or scrim administrator

Confirm both rosters, handle substitutes, enforce series rules, and preserve a
clean draft and result record.

### Player

Set role preferences once, join in one click, know when the lobby is ready, and
play again with people from a good match.

### Server administrator

Configure channels, roles, defaults, and permissions safely without editing
environment variables or JSON.

## Delivery phases

### Phase 0: production foundation

Complete this before promoting GodForge to unrelated servers.

#### Durable guild and orchestration state

- Persist guild settings, party lobbies, participants, role preferences, ready
  states, drafts, and match linkage.
- Recover live operations after restart and reconcile missing Discord
  messages/channels.
- Make state transitions idempotent and auditable.
- Use one production database-backed repository abstraction. Keep JSON only for
  local development or explicit import/export.

#### Guild authorization and setup

- Complete Discord OAuth guild permission verification.
- Require Manage Guild or a configured organizer role for administration.
- Detect whether GodForge is installed and provide the correct invite flow.
- Provide a guided `/setup` that creates or reuses the lobby panel channel,
  temporary-room category, organizer/captain roles, region, and default rules.
- Detect naming and permission conflicts before changing existing server
  resources.
- Provide a test mode with shortened timers, solo fill, and no persistent match
  statistics so administrators can verify the complete lifecycle.

#### Managed cosmetic roles

- Create permissionless GodForge roles for Solo, Jungle, Mid, Support, and ADC.
- Optionally create captain, substitute, region, and LFG-notification roles.
- Store returned Discord role IDs per guild; names are display metadata, not
  identity.
- Publish a persistent self-assignment panel that adds and removes roles.
- Reuse roles by stored ID, tolerate renames, and recreate explicitly deleted
  managed roles.
- Diagnose when GodForge lacks Manage Roles or sits below a managed role.
- Never adopt or delete an administrator-created role based only on a matching
  name.
- Keep database-backed matchmaking preferences authoritative. Discord roles
  remain the visible, cosmetic projection of those preferences.

#### Scope cleanup

- Remove legacy betting, wallet, ledger, and economy panels from the active
  GodForge experience.
- Replace economy-oriented match operations with a small match lifecycle:
  scheduled, lobby open, ready, drafting, playing, result pending, complete,
  or cancelled.
- Preserve ForgeLens code only as an isolated optional adapter with compatibility
  tests.
- Hide ForgeLens identifiers and status from normal workflows when disabled.

Exit criteria:

- A restart does not silently lose an active lobby or draft.
- OAuth login alone cannot grant guild administration.
- A fresh server can install and configure GodForge without source edits.

### Phase 1: Party Lobby MVP

This is the highest-priority market feature. **Historical design intent —**
**the panel buttons, creation modal fields, and card button list below were**
**superseded by the Issue #63 queue-first hardening pass. For current**
**behavior, see [`PARTY_QUEUE_UX.md`](PARTY_QUEUE_UX.md).**

#### Persistent Play SMITE panel

An administrator runs `/party setup` once. GodForge posts a durable panel with:

- Create lobby.
- Browse lobbies.
- Join queue.
- My preferences.

The panel survives individual lobby cleanup and can be refreshed without
duplication.

The queue itself is persistent and reusable across matches. Completing or
cancelling one match returns the panel to an available state rather than
requiring staff to recreate it.

#### Lobby creation and discovery

Use a Discord modal to capture:

- Mode, beginning with Conquest.
- Play now or scheduled.
- Region.
- Format: casual custom, balanced PUG, captains draft, or pre-made scrim.
- Party size, defaulting to ten.
- Optional experience or skill band.
- Voice requirement.
- Notes.

The lobby card shows open spots, waitlist, start time, organizer, and rules.
Buttons provide Join, Leave, Edit, Cancel, and Share.

#### Role preferences

Collect a player's:

- First-choice role.
- Second-choice role.
- Fill permission.
- Captain volunteer preference.

Store preferences per guild and allow players to revise them in the lobby.
Do not require a separate web account.

#### Capacity, waitlist, and substitutes

- Fill the active roster before an ordered waitlist.
- Promote a compatible substitute when someone leaves or fails ready check.
- Prefer promotions that preserve Jungle, Mid, ADC, Support, and Solo coverage.
- Notify affected users without repeated server-wide pings.

#### Ready check

- Start automatically at capacity or manually by the organizer.
- Provide Ready, Need 5 Minutes, and Drop actions.
- Apply a configurable timeout.
- Move timed-out players to the waitlist or remove them.
- Reveal private lobby credentials only after required players are ready.

#### Temporary rooms

- Create a lobby thread/text room and optional team voice rooms.
- Give the organizer limited lock, kick, transfer, and close controls.
- Clean up empty rooms after a grace period.
- Archive a lobby summary before deletion.

#### Draft transition

When ready:

- Form or confirm teams.
- Select captains.
- Start the existing GodForge draft without re-entering participants and
  options.
- Preserve the lobby, guild, participants, roles, rules, and GodForge match ID
  in the draft and match record.

Phase 1 non-goals:

- Cross-server queues.
- AI personality matching.
- Public reputation scores.
- Automated brackets.
- In-game rank verification.

### Phase 2: match quality and retention

#### Role-aware team formation

Support:

1. Role fit: maximize first- and second-choice satisfaction.
2. Balanced: minimize the team-strength difference while filling all roles.
3. Captains: draft players with role visibility.

Begin with transparent GodForge-owned inputs: organizer skill bands, optional
self-declared experience, role preference, and recent game-night history.
Explain the balance outcome instead of presenting an opaque AI score.

#### Standalone results and history

- Let both captains confirm the winner.
- Let an organizer resolve disagreement.
- Support cancelled and no-contest outcomes.
- Store teams, roles, draft reference, result, series score, timestamps, and
  participants.
- Show recent matches for a guild, team, or player.
- Provide recreational aggregate statistics such as appearances, wins, role
  frequency, teammate frequency, and streaks.
- Clearly scope these statistics to GodForge-run games.

Detailed performance statistics, OCR, screenshots, and automated ingestion are
optional future work. Economy and betting remain excluded.

#### Rematch and stay together

After completion, offer:

- Run it back with the same teams.
- Shuffle teams.
- Return to queue.
- Invite substitutes.
- Continue a best-of series.

#### Lightweight safety

- Private block/avoid lists.
- Organizer reporting with categories and evidence links.
- Per-guild notes and suspensions.
- Opt-in favorite teammate and recently played signals.

Do not launch a public global reputation number without formal moderation,
appeal, and privacy systems.

### Phase 3: scheduled nights and scrims

#### Scheduled sessions

- Natural-language time input with explicit timezone confirmation.
- Weekly templates.
- Capacity, role slots, waitlists, and reminders.
- Calendar export.
- Convert RSVPs into a live check-in lobby before the start.

Every scheduled event must end in a party, scrim, or draft workflow. GodForge
should not become a general calendar.

#### Teams and scrim challenges

- Guild-scoped teams with captain, roster, substitutes, region, and preferred
  time.
- Challenge, accept, reject, or propose a new time.
- Lock and check in rosters.
- Create private match rooms and launch the existing series/draft workflow.

#### Tournament integration

Integrate with an established bracket provider before building bracket logic:

- Import or link scheduled tournament matches.
- Map checked-in rosters to GodForge lobbies.
- Export GodForge match and draft references.

### Phase 4: network effects

Only after the single-server product has proven demand:

- Let guilds opt into a trusted cross-server lobby network.
- Share only region, mode, time, skill band, and open spots until a join is
  accepted.
- Preserve each guild's bans and safety policy.
- Rate-limit broadcasts and provide granular administrator controls.

Launch only after at least 20 active guilds, healthy local fill and completion
rates, and documented moderation, appeal, privacy, and retention policies.

## Implementation sequence

Each tracker issue should be a thin, independently verifiable vertical slice.

1. Establish the party-lobby domain model, durable state, and recovery path.
2. Isolate and feature-gate the optional ForgeLens adapter.
3. Complete permission-safe guild setup and publish a persistent Play panel.
4. Provision and reconcile self-assignable GodForge cosmetic roles.
5. Deliver create, browse, join, leave, and preference flows.
6. Add capacity, waitlist, compatible substitute promotion, and ready checks.
7. Create and reconcile temporary party/team rooms.
8. Convert a ready lobby into the existing fearless draft.
9. Add standalone result confirmation and match history.
10. Add role-aware balance and captains-based player drafting.
11. Add rematch, shuffle, substitute, and re-queue actions.
12. Add scheduled sessions that convert into live lobbies.
13. Add team rosters and scrim challenges.

New Discord interactions should be extracted from the already-large `bot.py`.
Use a Discord adapter/cog layer over pure party, balancing, and match services.
Model lobby transitions explicitly:

```text
open -> full -> ready_check -> forming -> active -> completed

open | full | ready_check | forming | active -> cancelled
open | full | ready_check                    -> expired
```

Discord message and channel IDs are delivery references, not domain identity.
Use reconciliation or outbox behavior for Discord side effects so retries and
restarts do not duplicate rooms or drafts.

## Success metrics

### Activation

- At least 60% of configured guilds create a lobby within 24 hours.
- Median setup time below five minutes.

### Party formation

- Median immediate lobby creation-to-ready time below 15 minutes in active
  communities.
- At least 70% of full lobbies become an active draft.
- Fewer than 15% of reserved seats become ready-check no-shows.

### Completion and retention

- At least 90% of started drafts reach a confirmed result or an explicit
  cancelled/no-contest state.
- At least 25% of completed lobbies choose rematch, shuffle, or re-queue.

### Reliability

- No silent loss of active lobby state during a routine restart.
- Fewer than 1% of lobbies require manual orphan cleanup.

Do not begin cross-server discovery until GodForge sustains 20 weekly active
guilds, 100 completed monthly lobbies, a four-week completion rate above 70%,
and a documented safety and appeal process.

## Optional ForgeLens compatibility policy

- Disabled by default.
- No required service, environment variable, identifier, or UI state.
- No ForgeLens failure can block a core GodForge workflow.
- Preserve compatibility tests for the portable draft JSON.
- Keep companion-specific fields out of core party, draft, and match models.
- Map core records to ForgeLens payloads only inside the adapter.
- If reactivated, begin with optional advanced statistics or historical
  enrichment.
- Do not reactivate wallets, wagering, odds, payouts, or betting as part of the
  integration.
