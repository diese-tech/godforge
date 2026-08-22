# Scheduled Custom Nights

GodForge scheduling is a narrow entry point into the existing party loop. It
does not manage general meetings or require a calendar account.

## Discord workflow

1. Run `/party schedule` with a natural time such as `Friday 8 PM` and an
   explicit IANA timezone such as `America/New_York`.
2. GodForge echoes Discord's normalized absolute and relative timestamps.
   Nothing is published until the organizer runs `/party confirm EVENT_ID`.
3. Confirming immediately posts a persistent **RSVP card** to the configured
   Play channel — no separate share step. The card shows the recurrence,
   the next occurrence's Discord-native timestamp, and the current
   Going/Maybe/Waitlist rosters with compact SMITE role context, and stays
   live for the whole RSVP window (all week, for a weekly night).
4. Players respond from the card's buttons — **Going**, **Maybe**, or
   **Can't Make It** — with no command required. A returning player with a
   saved primary role RSVPs in one click (with a **Change Roles** escape
   hatch); a first-time player sees the same lightweight Primary/Secondary/
   Fill picker used for ad-hoc queues. Maybe never occupies a seat or
   waitlist slot; going through the button flow saves the player's role
   preferences the same way joining a queue does. `/party rsvp EVENT_ID` and
   `/party unrsvp EVENT_ID` remain working fallbacks and refresh the same
   canonical card. Capacity overflow uses an ordered waitlist; releasing a
   seat promotes the earliest waiting player.
5. `/party calendar EVENT_ID` downloads a portable ICS file. Weekly nights
   contain an RRULE and create the next GodForge occurrence when opened.
6. GodForge sends configured reminder DMs once, with delivery claims persisted
   across restarts. Reminder offsets must be at least five minutes so the
   five-minute delivery poll cannot skip their entire window.
7. The organizer runs `/party open-scheduled EVENT_ID`. Retries resolve to the
   same ordinary party lobby, roster, and queue. Existing ready-check, draft,
   room, and results workflows apply unchanged. For a weekly night, the
   *same* RSVP card message rolls forward to the next occurrence with a
   fresh roster instead of a new card being posted; a one-time night's card
   gets one final edit into a "session started" state instead. RSVP history
   for the occurrence that just converted stays durable on its own row for
   audit purposes even though the public projection has moved on.

Use `/party events` to find event IDs and `/party unrsvp` to release a seat.
`/party session-refresh EVENT_ID` (organizer or a server manager) explicitly
reconciles one session's card — reposting it if missing, editing it in place
otherwise — without ever recreating the scheduled night itself. The same
reconciliation also runs automatically on bot startup and every periodic
cleanup pass, so a manually deleted card and restart recovery are both
self-healing.

## Supported time input

The deliberately small vocabulary is deterministic:

- `2026-08-01 8 PM`
- `tomorrow 20:00`
- `Friday 8:30 PM`

Timezone abbreviations such as `EST` are rejected because they do not identify
daylight-saving behavior. Scheduling depends on the pinned `tzdata` package so
the same input normalizes consistently on Windows and Linux.
Nonexistent spring-forward times and ambiguous fall-back times are rejected
with a repair message instead of silently shifting or guessing. Weekly
occurrences preserve the confirmed local weekday and wall-clock time across
daylight-saving offset changes. ICS exports use timezone-local recurrence
fields for the same reason.

## Data and scope

`scheduled_nights`, `scheduled_rsvps`, and `scheduled_reminders` share the
party SQLite database. Conversion uses stable lobby and operation IDs. No
OAuth token, external calendar account, generic attendee model, or separate
live-lobby state machine exists.

The RSVP card's channel/message IDs live on `scheduled_nights` itself
(`delivery_channel_id`/`delivery_message_id`); a weekly rollover carries
them to the newly-created next-occurrence row (tracked via
`predecessor_event_id`) rather than posting a second card. `scheduled_rsvps`
tags each response `going` or `maybe`; only `going` responses ever occupy a
seat or waitlist position.
