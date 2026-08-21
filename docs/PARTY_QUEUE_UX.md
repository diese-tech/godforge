# Party Queue UX

The Play panel is a queue-first entry point into the durable party lobby
system. This is the canonical reference for its current Discord surface,
lifecycle guarantees, and edge-case rules — see
[`ARCHITECTURE_REFACTOR_ROADMAP.md`](ARCHITECTURE_REFACTOR_ROADMAP.md) for how
`PartyLobbyService` is composed, and
[`MATCH_CONTINUITY.md`](MATCH_CONTINUITY.md) /
[`TEAM_FORMATION.md`](TEAM_FORMATION.md) for what happens after a match forms.

## Discord workflow

1. `/party setup` publishes a persistent Play panel with three buttons:
   **Start Queue**, **Find a Queue**, **My Roles**.
2. **Start Queue** opens a single-field modal (optional queue name only).
   Mode, region, format, capacity, voice requirement, and skill band default
   to sensible values (Conquest, 5v5, 10 players, no voice requirement, open
   skill) rather than being asked up front. A first-time organizer (no saved
   Primary role) then sees the same lightweight role picker a joining player
   gets, since they're seated as the queue's first participant; a returning
   organizer with saved roles goes straight from the name modal to a live
   queue. The canonical public card is auto-published to the configured Play
   channel immediately — there is no separate publish/share step.
3. **Join Queue**, on a specific queue's public card, always joins that exact
   queue. A first-time player is asked Primary role (required), Secondary
   role (optional — defaults to None without an explicit selection), and
   Fill (required). Captain willingness is not part of this picker; it stays
   editable from **My Roles** and only matters if an organizer later picks
   Captain Teams formation. A returning player with a saved Primary role
   joins in one click, with a **Change Roles** button on the confirmation.
4. **Find a Queue**, on the Play panel, joins immediately if exactly one
   queue is open. With more than one open, it presents an explicit choice
   (name, code, mode, roster count) — it never guesses which queue a player
   means.
5. A queue transitions to a ready check automatically the moment it fills;
   no organizer action is required. The ready-check card shows a **Waiting
   on** list of outstanding players (with a "needs 5 minutes" annotation
   where relevant) instead of only a count, and posting or refreshing it
   never itself sends a notification ping.
6. The final Ready response advances the queue automatically: it provisions
   the private match room, posts the formation-control card there, and sends
   **one** in-server message pinging the full roster with a real `<#channel>`
   mention to the new room. That is the only roster ping anywhere in the
   flow — normal operation never depends on DMs. The public card updates in
   the same pass to show "Match forming · Continue in #match-...".
7. Organizer-only actions (Rename, Edit Details, Repost Queue, Transfer
   Organizer, a manual Start Ready Check fallback) live behind an ephemeral
   **Queue Settings** panel reached from the public card, keeping that card
   itself down to Join / Leave / Queue Settings / Cancel.
8. Formation choice (Role Fit / Balanced / Captain Teams) happens with
   buttons in the private match workspace, never on the public card.

## Lifecycle rules

- **Stable identity.** Every queue has a `lobby_id` (internal identity, never
  shown), a short `queue_code` derived deterministically from it, and an
  optional organizer-set `display_name`. Renaming changes only the display
  name; the code and internal identity never change.
- **One active queue per player per guild.** A player already active in one
  queue (including waitlisted — waitlist membership is checked against the
  live queue state, not just the durable roster) is rejected from joining or
  starting another, with the existing queue's name/code and a **Leave That
  Queue** recovery action. This applies to Start Queue too, since creating
  seats the organizer as a participant.
- **Organizer succession.** If the organizer leaves an active queue,
  ownership transfers automatically to the longest-tenured remaining
  participant. If no participants remain, the queue is cancelled.
- **Recruiting inactivity confirmation.** A recruiting queue (OPEN/FULL) no
  longer hard-expires on silence. 60 minutes after its last meaningful
  activity — join, leave, rename, edit, or waitlist promotion each push the
  clock back — GodForge posts a one-time organizer-only prompt ("Still
  recruiting?" with **Keep Queue Open** / **Close Queue**) and starts a
  second 60-minute grace period. Any further meaningful activity during
  grace implicitly re-primes the queue (no click required) and retires the
  stale prompt. If nothing happens for the full grace period, the queue
  auto-closes and its public card is deleted rather than left showing a
  dead "cancelled"/"expired" status — the same deletion happens for an
  explicit **Cancel Queue** and for a ready-check timeout that cancels the
  lobby. This does not apply to READY_CHECK/FORMING/ACTIVE, which have their
  own separate deadlines.
- **Multi-queue isolation.** Concurrent queues in the same guild are fully
  independent: filling, ready-checking, or forming one never touches
  another's card, roster, or ready-check state.
- **Idempotent handoff.** The final Ready response is serialized per lobby
  so a double-click or retried interaction can't provision two rooms or send
  two roster pings; the roster ping is additionally gated on a durable flag
  so a retry after a failed send can still deliver it.
- **Restart recovery.** Every active lifecycle stage is reconciled after a
  restart: an OPEN/FULL queue's public card is reposted if missing, a
  READY_CHECK card is restored, and a FORMING match's formation card and (if
  it never went out) roster ping are re-sent.

## Data and scope

`PartyLobby` and its `DiscordDelivery` projection are stored by
`SQLitePartyRepository`; `queue_code` and `display_name` live as columns on
`party_lobbies`, while delivery references (public card, ready-check card,
match channel, and the one-time roster-ping flag) live inside the existing
`delivery_json` blob — no separate schema for them. The queue-membership
service (`PartyQueueService`) is the source of truth for active/waitlist
membership and ready-check state; the durable lobby record mirrors active
participants but not waitlisted ones, which is why the one-active-queue
guard consults both.
