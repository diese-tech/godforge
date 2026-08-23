# Godforge Website

Static landing page and admin portal for the Godforge Discord bot.

This folder is intentionally separate from the Python bot runtime. It does not change `bot.py`, `requirements.txt`, `Procfile`, environment variables, or deployment settings.

The dashboard runs locally or through the combined Railway launcher. Public
randomizer/build tools stay open, while admin actions use a temporary password
session or staged Discord OAuth session. Dashboard settings, audit events, and
custom command configs persist to JSON by default, or to SQLite when
`GODFORGE_STORAGE=sqlite` is enabled. Current gaps are tracked in `TODO.md`.

Product-level release milestones are tracked in `../VERSION_HISTORY.md`.
GodForge `v2.3.0-rc.5` establishes the standalone product boundary; the web
surface supports standalone utility, draft, settings, and command workflows.

Release process details live in `../RELEASE_PROCESS.md`. The bot-visible version is controlled by `GODFORGE_VERSION` in `../utils/formatter.py` and appears at the bottom of `.help`.

The optional local API in `../web_api` exposes a development bridge to the
GodForge parser, loader, picker, draft, settings, and custom-command modules.
When it is not running, the website remains previewable with utility demo data.

## Preview Locally

From the repo root:

```powershell
cd web
npm run dev
```

Then open:

```text
http://localhost:5173
```

You can also open `web/index.html` directly in a browser.

## Optional Shared-Logic API

In a second terminal, from the repo root:

```powershell
python web_api/server.py
```

The local API runs at:

```text
http://localhost:8787
```

With the API running, random god, Roll Team (`.roll5`), build generation,
command configuration, and draft tools reuse the same Python modules as the bot.

Admin-only dashboard actions require `GODFORGE_ADMIN_PASSWORD` when using `web_api/server.py` or the combined Railway launcher. Public randomizer and build endpoints remain available without login.

After login, the Overview panel includes bot status, guild count, active draft
rooms, a managed-server selector grid, module health, and recent admin activity.

The Settings module saves temporary guild defaults to
`data/guild_settings.json`. It is a staging surface until Discord guild
permissions and durable per-guild configuration are complete.

The Command Config module saves temporary custom command configs to `data/custom_commands.json` or SQLite. Unknown dot commands in Discord can execute these configs with enabled state, channel gates, role gates, per-user cooldowns, and mention suppression.

Dashboard settings, audit, and custom command configs can use SQLite instead of JSON by setting:

```text
GODFORGE_STORAGE=sqlite
GODFORGE_DB_PATH=/app/data/godforge_dashboard.db
```

JSON remains the default until the storage switch is explicitly enabled.

## Combined Railway Launcher

The fast live deployment path uses the repo-root launcher:

```powershell
python railway_app.py
```

It starts the web/API server on Railway's `$PORT` and runs the Discord bot in the same service so both use the mounted `/app/data` volume.

Current Railway public URL:

```text
https://godforge-hub.up.railway.app
```

Discord OAuth callback:

```text
https://godforge-hub.up.railway.app/api/auth/discord/callback
```

Discord OAuth client id:

```text
1493371999031136318
```

Do not commit the Discord client secret. Add it directly to Railway as `DISCORD_CLIENT_SECRET`.

OAuth Railway variables:

```text
DISCORD_CLIENT_ID=1493371999031136318
DISCORD_CLIENT_SECRET=<set in Railway>
DISCORD_OAUTH_REDIRECT_URI=https://godforge-hub.up.railway.app/api/auth/discord/callback
```

## Build

There is no compiled build step. This is plain HTML, CSS, and JavaScript.

```powershell
npm run build
```

Security helper coverage for dashboard HTML escaping:

```powershell
npm run test:security
```

Static dashboard coverage for tab/panel wiring and required admin surfaces:

```powershell
npm run test:dashboard
```

## Edit Points

- Keep Discord login links pointed at `/api/auth/discord/start`; add a separate bot invite URL when the install flow is ready.
- Keep dashboard copy aligned with live behavior as auth, storage, and guild permissions evolve.
- The randomizer currently uses SmiteFire CDN god portraits for visual context.
- Dashboard data shapes are documented in `DATA_CONTRACT.md`.
- Production asset slots and naming guidance are documented in `ASSET_MANIFEST.md`.
- The temporary password login should be removed once Discord OAuth plus guild permission checks fully gate admin actions.
- Settings, audit, and custom commands use JSON by default or SQLite when enabled; multi-guild production should keep SQLite/Postgres-style durable storage enabled.
- Temporary custom command configs persist through the dashboard and execute in Discord for unknown dot commands; future work should replace the simple role labels with Discord OAuth guild permission checks.
- Production graphics can be mapped into the named asset slots: `god-card`, `item-card`, `role-icon`, `dashboard-hero`, `background-texture`, and future in-game map surfaces.
