# DESK-005 WKWebView spike harness

Operator-run, manual. It exists so the WKWebView evidence run is a file rather
than someone's recollection, and so the human steps reduce to the acts that
genuinely need a human: one real click, one real drag, a device change, a sleep
and wake, and listening to the capture.

Read `docs/quality/desk-005-wkwebview-spike-runbook.md` first. It owns the
procedure; this file owns only how to get the harness into the WKWebView.

Provenance: written on the losing rebuild lane (PR #454), never merged to main,
ported here unchanged in behavior. Still NOT RUN.

## Files

| File | Role |
| --- | --- |
| `spike-criteria.mjs` | criterion registry (WKV-01 to WKV-13), verdict validation, gate rollup, markdown rendering. Pure functions; unit tested. |
| `harness.mjs` | installs `window.wkwebviewSpike`; drives the automatable criteria through the production dispatcher and samples state. |
| `inject.mjs` | bundle entry, so the harness can be pasted into a console. |

Unit coverage:
`apps/webui/frontend/tests/unit/wkwebview-spike-criteria.test.mjs`, run by the
frontend gate (`pnpm -C apps/webui/frontend test:unit`).

## Injecting it

The harness runs inside the WKWebView window that already has `/performance`
loaded. It creates no audio graph, loads no fixture, and mocks nothing: every
observation comes from `window.musicDjToolsPerformance` (the shared typed
dispatcher), from the real analyser via `capture(deck)`, and from the rendered
waveform's `aria-valuenow`.

Console paste, the path that works regardless of how the host serves assets:

```sh
cd apps/webui/frontend
pnpm exec esbuild tests/manual/wkwebview-spike/inject.mjs \
  --bundle --format=iife --outfile=/tmp/wkwebview-spike-harness.js
pbcopy < /tmp/wkwebview-spike-harness.js
```

Paste into the Web Inspector console attached to the WKWebView window.
Installation throws with a named `UNAVAILABLE:` reason if the performance IPC is
absent or reports a version other than 1, so a failed injection cannot look like
a clean run.

Options, set before pasting:

```js
globalThis.__WKWEBVIEW_SPIKE_OPTIONS__ = {
  activationMode: 'require-gesture',  // must match the build's activation mode
  apiBase: '',                        // set only when the API is not same-origin
  soakMs: 300000                      // four-deck soak; a gate run wants 5 min
};
```

`soakMs` defaults to 60 s so a first shakedown is quick. WKV-05 asks for at
least five continuous minutes, so a gate run sets `soakMs: 300000` and records
the value used.

For a Vite dev-server window, `await import('/@fs/<absolute path>/harness.mjs')`
also works, but dev-server evidence is triage only: a gate run needs the
production build inside the real shell. See the runbook, section 3, for how to
get the harness into a WKWebView that has no console.

## API

```js
await wkwebviewSpike.begin({ commit_sha, macos_version, webkit_version,
                             hardware, audio_device, activation_mode, operator });
wkwebviewSpike.startSampling();              // 10 Hz state and anomaly sampler
const gate = await wkwebviewSpike.runAutomatable();
wkwebviewSpike.humanSteps();                 // the acts only a human can do
wkwebviewSpike.mark('WKV-11', 'PASS', 'what you observed');
wkwebviewSpike.anomalies();                  // sampled clock/processor violations
wkwebviewSpike.stopSampling();
const { json, markdown, gate } = wkwebviewSpike.dump();
```

`begin` requires the full host metadata block and fails fast on a missing
field, so no evidence can exist that is unattributable to a commit and a
machine. `mark` refuses a second verdict for the same criterion: a retry keeps
the first failure and belongs in a new run document.

## What it deliberately does not do

- It does not decide whether WKWebView supports anything. Nothing in this
  directory was observed on WKWebView; the registry's `expectation` fields say
  where each expectation comes from.
- It does not synthesize audio, stub a decoder, or fabricate state. If a
  capability is missing it records UNAVAILABLE naming it.
- It does not prove sound reached the physical output. `capture(deck)` reads the
  real analyser inside the real graph; output truth is the loopback WAV.
- It does not save files. Whether a WKWebView download or clipboard write
  succeeds is an expectation, not a known, so `dump()` prints and returns, and
  the operator saves into the run directory.
- It does not automate the drags, the device change, the sleep and wake, or the
  activation click. Synthesized input would be a mock of the exact thing under
  test.
