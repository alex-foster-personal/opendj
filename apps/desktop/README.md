# apps/desktop -- Open DJ desktop shell

Tauri 2 native window wrapping the local engine's web UI. This is a scaffold
(compiles via `cargo check`; `cargo tauri build` has not been exercised yet).

## Thin-shell rule (non-negotiable)

This app is a **window, not an application**. All product logic lives in the
engine (FastAPI daemon + SvelteKit webui) served over HTTP at
`http://127.0.0.1:8683`. The Tauri window just points at that URL and gets
out of the way.

Concretely:

- **No `@tauri-apps/api` in the webapp.** The SvelteKit frontend served by the
  engine must not import from `@tauri-apps/api/*`. It should render and
  behave identically whether it's opened in a Tauri window or a plain browser
  tab. If a feature needs desktop-only behavior, it does not belong in the
  webapp -- it belongs behind a capability check the engine already exposes
  over HTTP, or it is out of scope for this shell.
- **No Tauri IPC commands for app logic.** `src-tauri/src/main.rs` does not
  define `#[tauri::command]` handlers that implement business logic. If the
  shell ever needs a native-only capability (e.g. a system file picker), that
  capability must stay a thin pass-through -- it forwards to the engine or
  returns raw OS data, never makes product decisions.
- **Rust owns the window, nothing else.** No state, no business rules, no
  data access in `src-tauri/`. If you find yourself reaching for `rusqlite`,
  `reqwest`, or anything that talks to the engine's data layer from Rust,
  that logic belongs in the engine instead.

Rationale: the engine is the single source of truth and is already
browser-addressable. Duplicating logic into the shell would fork behavior
between "desktop app" and "open the URL in a browser" -- exactly the
maintenance trap this thin-shell rule exists to avoid.

## Current state

- `src-tauri/tauri.conf.json` points the single window at
  `http://127.0.0.1:8683` (the engine URL). The port is hardcoded for now --
  **TODO: read it from the same config the engine uses instead of hardcoding
  it.**
- `bundle.active` is `false`; this scaffold has only been verified with
  `cargo check`, not a full `cargo tauri build`.
- Icons under `src-tauri/icons/` are generated from the existing open-dj
  brand mark (`apps/webui/frontend/static/icon-512.png`), converted to RGBA
  (Tauri's `generate_context!` requires RGBA source icons; the brand PNG is
  opaque RGB).

## Build

```sh
cd apps/desktop/src-tauri
cargo check          # verified working
cargo tauri build     # not yet exercised -- needs `cargo install tauri-cli` first
```

productName: `Open DJ`
identifier: `com.opendj.desktop`
