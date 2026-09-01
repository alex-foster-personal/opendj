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

## Bundle budgets

`scripts/check-bundle-size.sh` runs post-build in CI and enforces one gzip
budget per SURFACE, measured from the real module graph. Measurements are JS
under `build/_app/immutable/` (CSS is emitted as an asset, not a chunk, and is
outside these budgets):

| budget        | limit   | measured on `c3cf9329` | what it covers                                      |
| ------------- | ------- | ---------------------- | --------------------------------------------------- |
| `library`     | 256,000 | 93,011 (36.3%)         | the initial load of `/`                              |
| `performance` | 203,776 | 193,544 (95.0%)        | `/performance` and its children, lazily loaded       |
| `other-lazy`  | 67,584  | 64,328 (95.2%)         | every other route, plus deferred app-shell chunks    |

A surface is the STATIC import closure of its roots. SvelteKit code-splits at
every dynamic import, so a dynamic import is a budget boundary: weight behind
one is not charged to a page that never takes that branch. A file reachable from
several surfaces is charged to the first that reaches it, in the order above, so
nothing is double counted.

Two properties matter more than the numbers:

- **Nothing is unmeasured.** Every `.js` emitted under `_app/immutable/` must
  land in exactly one budget. A chunk that no surface reaches and no surface
  names FAILS the run. AudioWorklet processors are fetched by URL rather than
  imported, so they are charged to the surface whose code names them.
- **Every budget is proven to fail.** `tests/unit/bundle-budget.test.mjs`
  constructs a build state that trips each budget in turn, plus the
  unattributed-chunk case, against a synthetic build tree in a temp dir. No
  `pnpm build` needed; it runs in `pnpm test:unit`.

The `library` figure of 256,000 is unchanged from the gate's introduction. The
other two are ratchets at their measured value plus 5%, rounded up to the next
KiB. `other-lazy` in particular is tight by construction (about 3 KB of
headroom across 16 routes); widen it deliberately if ordinary feature work trips
it, and record the new derivation in `scripts/bundle-budget.mjs`.

Machine-readable totals (still exits non-zero if a budget is breached):

```bash
pnpm build && bash scripts/check-bundle-size.sh --json
```
