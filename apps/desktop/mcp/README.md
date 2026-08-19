# webview-mcp -- drive and see inside the REAL Tauri WKWebView, cheaply

The iteration-speed fix for the desktop shell: an MCP server any agent
(Claude Code, Codex, Cursor) can plug in to observe and drive the running
DEBUG shell without screenshots-by-hand, without rebuilding a dmg, and at
token costs chosen per call.

## How it works

The debug binary already embeds a W3C WebDriver HTTP server
(`tauri-plugin-wdio-webdriver`, `cfg(debug_assertions)` -- the dmg has zero
webdriver surface). This MCP is a thin token-shaping layer over that seam plus
the engine's own HTTP API. No new Rust, no new release surface.

## Tools (cheapest first -- escalate, don't default upward)

| Tool         | What                                                      | Rough cost |
|--------------|-----------------------------------------------------------|------------|
| `app_state`  | engine JSON (GET /api/v1/*)                               | 0.2-0.6k tok |
| `ui_tree`    | a11y-style DOM tree with @refs, version-stamped           | 0.5-4.5k tok |
| `screenshot` | native WKWebView snapshot, retina-descaled, croppable     | ~1.6k tok  |
| `act`        | click/type/chord by @ref; auto-returns the tree DIFF      | diff-sized |
| `navigate`   | point the shell at a route                                | tiny       |
| `eval_js`    | escape hatch, raw JS in the page                          | varies     |
| `status`     | both seams healthy + current page                         | tiny       |

Honest-delivery rule: `act` dispatches synthetic DOM events and says so in
every result. The embedded driver's native input path drops hover, dblclick,
and some chords (measured in tier 2) -- synthetic reaches the same window-level
capture listeners the app installs (see `hotkeys.ts`), so nothing is silently
substituted.

Staleness rule: refs die when the tree changes. `act` on an old `tree_version`
returns an explicit "re-observe" error instead of clicking the wrong thing.

## Run it

1. Start a debug shell with the webdriver enabled and an engine to point at:

```bash
cd apps/desktop/src-tauri && cargo build
OPENDJ_ENGINE_ORIGIN=http://127.0.0.1:<engine-port> TAURI_WEBDRIVER_PORT=4456 \
  ./target/debug/opendj-desktop
```

2. Register the MCP with your agent (Claude Code shown):

```bash
claude mcp add opendj-webview \
  -e MDT_WEBVIEW_MCP_WEBDRIVER=http://127.0.0.1:4456 \
  -e MDT_WEBVIEW_MCP_ENGINE=http://127.0.0.1:<engine-port> \
  -- pnpm --dir apps/desktop exec tsx mcp/webview-mcp.ts
```

Ports are explicit everywhere -- no hidden defaults. Check the reserved-port
list before picking one (`wdio.conf.ts` documents it; smoke uses 8698/4456).

## Verify

```bash
just webview-mcp-smoke
```

Boots a fixture engine + debug shell + the MCP over real stdio and asserts the
whole loop, including stale-ref rejection and retina descaling. No mocks.

## When NOT to use this

- Iterating on UI/logic: use tier 0 (vite dev in a browser) -- it is faster.
- Gesture-level coverage (hover, dblclick): tier 1 Playwright webkit owns it.
- Anything against a release build: impossible by design, and should stay so.
