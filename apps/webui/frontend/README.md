# apps/webui/frontend -- SvelteKit SPA (Phase 11)

SvelteKit 2 + Svelte 5 runes + adapter-static. The root worktree `.env` owns
its frontend and backend ports. Vite derives the API proxy from the backend.

## Quickstart

```bash
# From the repository root, using Node 22.14 or newer.
just run
# Or frontend only:
just webui-ports
just webui-frontend
```

`just run` opens one macOS Terminal per server (tmux on Linux) and then
`http://127.0.0.1:<frontend>`. Agentbox over Tailscale is
`just run-agentbox`. Details: `apps/webui/README.md`.

Direct `pnpm dev` also claims the repository-root `.env` through the shared
allocator, uses `strictPort`, and fails instead of selecting another
worktree's port.

## Build for production

```bash
pnpm api:gen     # optional: regenerate types from ../openapi.json
pnpm build       # writes apps/webui/frontend/build/
```

The FastAPI daemon in `apps/webui/server/app.py` auto-mounts this directory at
`/`, so `python -m apps.webui.server` serves the SPA on the backend endpoint
printed by `just webui-ports` once the build exists.

## Pages

- `/` library browser (virtual scroll, filter, sort)
- `/track/:stable_id` track detail + provenance tooltip + star rating +
  tags + notes; conflict dialog on 409
- `/playlist/:id` playlist diff viewer (fixture until Phase 3/4 merges)
- `/pairings` CRUD editor
- `/queues` read-only dedup / bad_beatgrid / auto_cue triage

## E2E tests

```bash
pnpm test:e2e
```

Run against a running dev daemon + SvelteKit dev server. CI skips if no
headless Chromium is available.

## Bundle budget

`scripts/check-bundle-size.sh` enforces a 250 KB total-gzipped budget on
the library page chunks; wire it into CI post-build.
