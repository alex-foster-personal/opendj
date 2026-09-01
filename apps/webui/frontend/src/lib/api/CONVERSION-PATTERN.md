# Converting a fetch module onto the generated OpenAPI client

The frontend used to hand-roll `fetch()` in ~25 modules, each repeating the URL
as a template string and re-inventing error handling. `src/lib/api-types.ts` is
now generated from the daemon's OpenAPI document (`pnpm api:gen`), and
`src/lib/api/client.ts` is the one runtime client over it. This file is the
recipe for moving a module across. It is meant to be executed literally, one
module per agent, without further design decisions.

Two conversions are already done. Read them before you start; they are shorter
than this document:

- `src/lib/reconcile-api.ts` (GET with query, GET with path param, POST with a
  CAS header, typed error class) plus `tests/unit/reconcile-api-base.test.mjs`
- `src/routes/dedup/dedup-api.ts` (POST, ETag read off the response, 409 as a
  workflow outcome, AbortSignal, hand-written runtime validators kept) plus
  `tests/unit/dedup-api.test.mjs`

## The import surface

```ts
import { API_BASE, ApiError, api, unwrap } from '$lib/api/client';
// inside src/lib itself, use a relative import: '../api/client' etc.
```

| Export     | Use it for |
| ---------- | ---------- |
| `api`      | the one client instance. `api.GET`, `api.POST`, `api.PUT`, `api.PATCH`, `api.DELETE`. |
| `unwrap`   | narrowing a call to its success body: `return unwrap(api.GET(...))`. |
| `ApiError` | `instanceof` checks when a module maps failures onto its own error type. |
| `API_BASE` | building a URL string for an `<img src>` or `<audio src>`, never for a fetch. |

The client throws on every non-2xx, so converted code has no `if (error)`
branch. `ApiError` carries `status`, `code`, `message`, the raw `response`
(headers intact) and the parsed `body`.

## The recipe

1. **Find the schema path.** Grep the literal path in `src/lib/api-types.ts`:
   `grep -n '"/api/v1/your/path"' src/lib/api-types.ts`. Path parameters appear
   in brace form, e.g. `"/api/v1/relocate/{stable_id}/apply"`, and that brace
   form is the string you pass to `api.GET` / `api.POST`. If the path is not
   there, STOP: the route is missing from the server's OpenAPI document (this
   is real, `/api/v1/pairings/sync-snapshots` is one). Report it and leave the
   module alone. Never hand-roll a fetch around a gap.

2. **Delete the module's local `request()` / `_fetchJson()` helper.** Its job
   (base URL, JSON headers, error decoding) is the client's job now.

3. **Rewrite each call.** Everything the URL used to carry moves into `params`:

   ```ts
   // before
   const r = await request(`/api/v1/relocate/candidates/${encodeURIComponent(id)}?limit=${limit}`);
   if (!r.ok) throw new Error(`get candidates failed: ${r.status}`);
   return r.json();

   // after
   return unwrap(
     api.GET('/api/v1/relocate/candidates/{stable_id}', {
       params: { path: { stable_id: id }, query: { limit } }
     })
   );
   ```

   - `params.path` values are URL-encoded for you. Remove `encodeURIComponent`.
   - `params.query` drops `undefined` and `null` keys and emits no `?` when the
     result is empty, which matches the old conditional query-string building.
   - `params.header` carries per-call headers: `params: { header: { 'If-Match': etag } }`.
   - `body` is the request object itself, NOT `JSON.stringify(...)`. The client
     serializes it and sets `Content-Type: application/json`.
   - `signal`, and any other `RequestInit` field, sits alongside `params`:
     `api.GET(path, { params: {...}, signal })`.
   - When you need the response headers (ETag, x-bind-warning), destructure
     instead of unwrapping: `const { data, response } = await api.GET(...)`.
   - `Accept: application/json` is dropped on purpose. The daemon does not
     content-negotiate (no route reads the Accept header), and the client sets
     `Content-Type` itself when there is a body.

4. **Replace hand-written response interfaces with generated aliases**, keeping
   the exported names so call sites do not move:

   ```ts
   import type { components } from '../api-types';
   export type BrokenTrack = components['schemas']['BrokenTrackOut'];
   ```

   Do this only when the fields match exactly. If they do not, the module and
   the server have already drifted: keep the hand-written type, and say so in
   the PR body. Do not "fix" the shape by editing the generated file, which is
   overwritten by `pnpm api:gen`.

5. **Map errors onto whatever the module already threw.** The exported error
   contract is part of the module's signature; do not change it.

   ```ts
   catch (error) {
     if (error instanceof ApiError) throw new RelocateApplyError(error.status, error.code, error.message);
     throw error;
   }
   ```

   | Old shape | New shape |
   | --------- | --------- |
   | `if (!r.ok) throw new Error(...)` | delete it; the client threw already |
   | `if (r.status === 409) { ... r.headers.get('etag') }` | `if (error instanceof ApiError && error.status === 409) { ... error.response.headers.get('etag') }` |
   | `const body = await r.json(); body.current` | `error.body` (already parsed) |
   | `try { await fetch(...) } catch { throw new Error('daemon unreachable') }` | keep the try/catch, but check `instanceof ApiError` first so a real HTTP answer is not mislabelled as unreachable |

6. **Run the gates** (below) and commit.

## What NOT to touch

- **Runtime validators.** Modules that parse a response field by field
  (`dedup-api.ts`, `smartlists`) keep doing it. Generated types are erased at
  runtime; they are not a guarantee about the bytes on the wire, and the
  irreversible workflows depend on the runtime check. Convert the transport,
  feed `data` into the existing validator.
- **Rune modules' caching and state.** `*.svelte.ts` files hold `$state` /
  `$derived` caches, in-flight maps, LRU eviction and prefetch scheduling.
  Convert the single fetch call inside; leave the surrounding cache logic
  byte-identical.
- **AbortController patterns.** Keep the caller's controller, the sequence
  counters, the `signal.aborted` re-checks after an await. Only pass the signal
  through: `api.GET(path, { signal })`.
- **`src/lib/rb/api-rb.ts`.** Integrator-owned hotspot, excluded from this
  workstream entirely.
- **ArrayBuffer / binary paths.** Audio, artwork and waveform bytes
  (`audio-prefetch-cache.svelte.ts`, `audioUrl()`, artwork `<img src>`) stay on
  raw `fetch`. This client parses JSON; the OpenAPI document does not describe
  those response bodies. `API_BASE` is exported for building those URLs.
- **Non-daemon origins.** `local-agent.ts` talks to a separate local agent
  process, not the FastAPI app. It is not in the schema and does not convert.
  Same for liveness probes against `window.location.origin`.
- **`src/lib/api.ts` converted LAST, and is now done.** Every other module
  imports `API_BASE` from it, so it went after them all; its `API_BASE` is now
  a re-export of the client's and the duplicate resolution is gone. `client.ts`
  still resolves the base URL itself rather than importing `api.ts`, because it
  is the root of the dependency graph and that import would be a cycle.
  Step 4 is now DONE there too (issue #725): every response type in that module
  is an alias of `components['schemas'][...]` and the module holds zero
  `as unknown as`. The drifts step 4 says to keep hand-written were re-read
  against the schemas and every one of them was the frontend being WRONG, not
  the two shapes being genuinely different -- including a non-optional
  `PlaylistDetail.updated_at` the server has never sent. Read that module as
  the worked example of step 4 at scale, and `requireBody` in `client.ts` as
  what a call site that needs a response header uses instead of asserting the
  body onto its type.

## Test-side changes (this bites every module)

`openapi-fetch` calls `fetch(request)` with a single `Request` object, where
the old code called `fetch(url, init)`. Unit tests that intercept
`globalThis.fetch` must move to the Request idiom:

```js
// before
globalThis.fetch = async (input, init) => { seen = { input: String(input), init }; ... };
assert.equal(seen.input, `${API_BASE}/api/v1/dedup/clusters`);
assert.equal(new Headers(seen.init.headers).get('if-match'), REVISION);
assert.deepEqual(JSON.parse(seen.init.body), { ... });

// after
globalThis.fetch = async (request) => { seen = request; body = await request.clone().json(); ... };
assert.equal(seen.url, `${API_BASE}/api/v1/dedup/clusters`);
assert.equal(seen.headers.get('if-match'), REVISION);
assert.deepEqual(body, { ... });
```

Cancellation cannot be asserted by object identity any more: `new Request(url,
{ signal })` creates a new signal that follows the caller's. Assert the
behaviour instead, which is a stronger test:

```js
assert.equal(seen.signal.aborted, false);
controller.abort();
assert.equal(seen.signal.aborted, true);
```

Two client behaviours exist so that this works at all, do not "simplify" them
away: the client forwards `fetch` through a closure (`openapi-fetch` otherwise
captures `globalThis.fetch` at import time, before a test can swap it), and
`unwrap` exists because `data` is typed as possibly-undefined to model an error
branch the throwing middleware has already passed.

## Checklist per module

- [ ] every path used by the module exists in `src/lib/api-types.ts`
- [ ] local `request()` / `_fetchJson()` helper deleted
- [ ] `grep -c 'fetch(' <module>` returns 0
- [ ] exported function signatures unchanged (call sites are NOT edited in this
      commit; if a call site has to change, you have changed a signature, stop)
- [ ] exported error classes still thrown for the same conditions
- [ ] response interfaces aliased to `components['schemas'][...]` where the
      fields match exactly
- [ ] runtime validators, caches and AbortController logic untouched
- [ ] the module's unit test updated to the Request idiom and green
- [ ] `pnpm run check` reports 0 errors
- [ ] conventional commit, body ends with `-Claude` then the `Co-Authored-By`
      trailer

## Definition of done

```sh
cd apps/webui/frontend
pnpm run check                                              # 0 ERRORS (18 warnings is the baseline)
node --test tests/unit/<module>.test.mjs                    # green
grep -c 'fetch(' src/<module>.ts                            # 0
```

A module is done when all three hold and nothing outside the module and its
test changed.

## Worklist

Regenerate the live list (the count shrinks as the fleet lands conversions).
The character class matters: a plain `grep 'fetch('` also counts
`ensureAudioPrefetch(` and friends.

```sh
cd apps/webui/frontend
grep -rcE "(^|[^A-Za-z0-9_])fetch\(" src --include='*.ts' --include='*.svelte' \
  | grep -v ':0$' | sort -t: -k2 -rn
```

Measured at `0711c179` that is 24 files / 41 raw call sites (the lane branch is
moving, so trust the command, not this number), of which four files never
convert:

| File | Sites | Why it stays |
| ---- | ----- | ------------ |
| `src/lib/api/client.ts` | 2 | the sanctioned fetch site (the late-binding closure) |
| `src/lib/rb/api-rb.ts` | 7 | integrator-owned hotspot, out of this workstream |
| `src/lib/rb/audio-prefetch-cache.svelte.ts` | 1 | ArrayBuffer audio body |
| `src/lib/rb/local-agent.ts` | 2 | separate local-agent process, not in the schema (and deleted as dead code later on the lane branch) |

That left **20 files / 29 call sites** as the fleet worklist when this was
recorded. Inline `fetch` inside a `.svelte` component (`BrowserPanel.svelte`,
`SuggestNextStrip.svelte`) is converted the same way, in place, without moving
the call into a new module. One known snag in that group: `BrowserPanel.svelte`
probes `window.location.origin` as a liveness check, which is not an API call
and stays.

**Worklist delta - the pairings phantoms are retired, not pending.** Step 1 says
to report a missing route and leave the module alone. For these two the report
came back as "no server implementation on any mainline branch", so they were
retired rather than parked:

- `CreatePairingSheet.svelte` (-2 sites): both targeted
  `/api/v1/pairings/sync-snapshots`. Capture, Align hotcues and Reload sync are
  now inert controls carrying `not implemented - see PARITY-TODO` and fire no
  request. The deck picker reads real engine state and stays live.
- `pairing-alignments.svelte.ts` (-1 file, -2 sites): GET + POST against
  `/api/v1/pairings/alignments`. Deleted outright - `CreatePairingSheet` was its
  only importer.

A capture router and its repo do exist on two archive branches (`718cc812`,
`6f39ab99`) but were never registered in `app.py`, so nothing ever served these
paths. The restore item is in PARITY-TODO.
