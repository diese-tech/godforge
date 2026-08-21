# GodForge Release Process

Use this checklist whenever GodForge moves from a release candidate to a tagged release.

## Version Sources To Update

- `utils/formatter.py`
  - `GODFORGE_VERSION`
  - This appears at the bottom of the `.help` embeds.
- `VERSION_HISTORY.md`
  - Canonical product milestone notes.
- `README.md`
  - Public bot command/version notes.
- `web/README.md`
- `web/index.html`
  - Dashboard deployment, storage, and OAuth notes.
- `web/TODO.md`
  - Move completed work out of future/staged sections.
- `web_api/README.md`
  - API/env/storage notes.

## v2.1.0 Release Gate — Completed

All gates passed. v2.1.0 shipped with the live dashboard bridge, Discord OAuth, SQLite storage, and admin surfaces.

## v2.2.0 Release Gate — Completed

All gates passed. v2.2.0 shipped with the ForgeLens handoff contract, orchestration bot hardening, CSRF protection, crash fixes, and dependency pinning. See `VERSION_HISTORY.md` for full scope.

## v2.3.0 Release Gate — In Progress

Tag `v2.3.0` only after:

- [x] Durable party lifecycle and restart recovery are merged.
- [x] Zero-config per-guild setup and managed cosmetic roles are merged.
- [x] Captain-confirmed results and guild-scoped game-night history are merged.
- [x] Scheduling, continuity, team formation, and scrim workflows are merged.
- [x] Active help/command surfaces describe standalone GodForge only.
- [x] Public web and API documentation do not promote guarded legacy economy
  surfaces as current features.
- [x] Optional companion compatibility remains disabled by default.
- [x] Version strings and release documentation agree at `v2.3.0-rc.4`.
- [x] Legacy web/API surfaces have an explicit removal or archive decision.
- [x] The active dashboard no longer renders economy, betting, wallet, or ledger
  controls; retained rollback code remains inaccessible by default.
- [ ] A live Discord smoke test confirms setup, restart recovery, and permission
  failure messaging.
- [x] Full local tests pass before push.
- [ ] Required GitHub checks are green on the `rc.4` candidate PR.
- [ ] Railway health and the public tools URL pass a live smoke test.

## Tagging

When a release gate passes:

```powershell
git tag v<version>
git push origin v<version>
```

Do not tag a release candidate as stable until the live smoke test passes.
