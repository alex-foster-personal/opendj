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

## Storybook -- the visual decision record

```bash
pnpm storybook         # dev server on http://localhost:6006
pnpm build-storybook   # static build into storybook-static/ (gitignored)
```

Storybook is not here to be a component gallery for its own sake. It is where
UI decisions are recorded **visually**: a story shows what a pattern looks
like in every state that matters, and an MDX decision doc beside it records
why the rule exists and what to do when adding new UI.

Two halves, both in the sidebar:

- **Components/** -- stories for presentational components, one story per
  state worth arguing about.
- **UI decisions/** -- the rules themselves, in
  `src/stories/ui-decisions/`. Each doc states the decision, why it exists,
  a checklist for new UI, where the rule stops, a live `<Canvas>` example,
  and how a breach is caught. Start from `_TEMPLATE.mdx`.

The first two entries are the house rules from `CLAUDE.md`: numeric readouts
carry hover titles, and controls without a real data source render inert with
the `not implemented - see PARITY-TODO` tooltip.

### The rule

**A new UI pattern gets a story plus a decision entry.** A one-off tweak does
not; a rule other components will be expected to follow does. If you find
yourself explaining a convention in a PR comment, that is the signal it
belongs here instead, where the next person will find it.

### Scope: presentational components only

Stories cover components that render entirely from props. Anything that
fetches is deliberately out of scope, because the house rule forbids mocked
APIs and wiring MSW to satisfy Storybook would break it. Extract the
presentational part first, then story that.

Two constraints worth knowing before adding stories:

- `src/lib/rb/theme.css` scopes every `--rb-*` var under `.perf-root`, so
  `.storybook/preview.ts` puts that class on `<body>`. Without it the rb
  components render unstyled and `wave/render.ts` throws on an empty var.
- Svelte components are values, so CSF metadata needs
  `satisfies Meta<typeof MyComponent>`, not `Meta<MyComponent>`.

### Future work: visual regression

Chromatic (or any screenshot-diffing service) would turn these stories into
a visual regression gate on every PR. **Deliberately not installed**: it is a
paid cloud service and the payoff is low while the UI is still moving this
fast. The stories are written so that adopting it later needs no rewrite.

## E2E tests

```bash
pnpm test:e2e
```

Run against a running dev daemon + SvelteKit dev server. CI skips if no
headless Chromium is available.

## Bundle budget

`scripts/check-bundle-size.sh` enforces a 250 KB total-gzipped budget on
the library page chunks; wire it into CI post-build.
