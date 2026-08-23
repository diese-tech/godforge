# GodForge Web Data Contract

This document covers the supported standalone web surface in
`v2.3.0-rc.5`. Detailed migration-era economy contracts are archived at
[`../docs/archive/LEGACY_WEB_DATA_CONTRACT.md`](../docs/archive/LEGACY_WEB_DATA_CONTRACT.md).

## Public utilities

- `GET /api/health`
- `GET /api/gods/roll`
- `GET /api/gods/roll5`
- `GET /api/builds/roll`

Invalid role, source, count, or god input returns a JSON error with an
appropriate `4xx` status.

## Authentication

Login, logout, auth status, and staged Discord OAuth routes manage dashboard
sessions. Protected mutations require both an authenticated session and the
matching CSRF cookie/header pair.

## Drafts

Draft endpoints are `/api/draft/start`, `/api/draft/action`,
`/api/draft/undo`, `/api/draft/next`, and `/api/draft/end`.

GodForge owns `draft_id`. Draft records carry guild/channel context, game
number, teams, bans, picks, claims, timestamps, and fearless-pool state.
Optional compatibility mapping occurs outside this core contract.

## Guild configuration and commands

- `GET/POST /api/settings`
- `GET/POST /api/commands/custom`
- `POST /api/commands/custom/delete`
- `POST /api/command`
- `GET /api/admin/status`
- `GET /api/admin/audit`

Custom commands are keyed by guild and include trigger, response, enabled
state, channel scope, role gate, cooldown, and audit timestamps.

## Unsupported legacy compatibility

Migration-era match, betting, wallet, and ledger endpoints remain in code only
as a failsafe. Mutations are rejected unless
`GODFORGE_ENABLE_LEGACY_ECONOMY=true`; they are not supported standalone
features and must not be presented in current user workflows.
