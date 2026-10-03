# The Chrome dev loop: run the UI from source against a dev engine, no DMG

This is the fast local feedback
loop for UI and transport work: edit a Svelte or TS file, see it in about a second, commit
from the same worktree, and let the queue carry the fix into the next DMG. It is the
practical half of DEVLOOP-01 (issue #850); the documented `dev-attach` recipe that points
the *shipped shell* at a dev engine is the other half and lives in `apps/desktop/README.md`
once that issue lands.

## Topology, and what is duplicated versus shared

```
 SHIPPED APP (Open DJ.app)                        CHROME LOOP
 =========================                        ===========

 [Tauri shell, Rust]                              [your real Chrome]
   | spawns                                          |  http://127.0.0.1:<frontend-port>/performance
   v                                                 v
 [bundled engine, python payload]                 [vite dev :<frontend-port>]  <-- serves src/ LIVE, HMR ~1 s
   | serves static UI from payload                   |  proxies /api  --------------------+
   | listens on a random port                        |                                    |
   v                                                 v                                    v
 [WKWebView]                                      (Chromium engine, NOT WebKit)     [dev engine :<backend-port>]
                                                                                     .venv/bin/python -m apps.engine_core serve
   reads/writes                                                                       |
   v                                                                                  v
 ~/Library/Application Support/                    ~/Library/Application Support/
   com.opendj.desktop/            --- rsync --->     com.opendj.desktop.chrome-loop/
   state.db  (hot cues, playlists, prefs)            state.db          (COPY, diverges from now on)
   master.plain.db, master.db.copy (rekordbox)       master.plain.db   (COPY, required or the browser
   anlz-cache/, feedback/                            anlz-cache/, feedback/ (COPY)   panel fails MASTER_DB_UNAVAILABLE)

 SHARED, NOT DUPLICATED
   ~/Music/... audio files              read in place by both engines, never written
   stem bundles (repo data/state/)     symlinked into the loop copy, immutable per track
   the worktree checkout                ONE checkout: vite serves its src/, the dev engine runs its apps/
   git history, PRs, the queue          unchanged; a fix here is committed from that worktree

 DUPLICATED ON PURPOSE
   engine process               separate Python process, its own port and lock file
   library data dir             separate rekordbox DB copy; loop edits stay in that copy
   browser engine               Chromium here, WebKit in the app: same code, different audio stack
```

The review loop uses `scripts/dev_loop_preflight.py`, `scripts/run_chrome_loop.sh`,
this document, and one disposable worktree. A new machine follows the recipe below
by hand or through `run_chrome_loop.sh`. Changes still follow the repository's
review and merge gates.

## Recipe

All commands run inside the worktree you are iterating on.

1. **Ports.** The repo assigns each worktree a backend/frontend pair from a registry in the
   git common dir; `MUSIC_DJ_*_PORT` env overrides are refused by the claim step, so use the
   pair it prints:

   ```bash
   uv run --no-sync python -m apps.webui.port_config claim
   ```

2. **Library copy.** Isolate the loop from the real library. The two rekordbox master files
   must be included or the browser panel fails with `MASTER_DB_UNAVAILABLE`:

   ```bash
   SRC="$HOME/Library/Application Support/com.opendj.desktop"
   DST="$HOME/Library/Application Support/com.opendj.desktop.chrome-loop"
   rsync -a --exclude 'logs/' --exclude '.engine.lock' --exclude 'state/state.db-wal' \
         --exclude 'state/state.db-shm' --exclude 'state/*.bak-*' "$SRC/" "$DST/"
   ```

   Re-run it (with the dev engine stopped) whenever the real library has moved on.

   **Why a separate data dir at all.** One engine per data dir is enforced by
   `.engine.lock`, so the loop engine could not start against the app's dir while the app
   runs. More importantly the loop runs unmerged code: a PR carrying a `state.db` migration
   would rewrite the real library the moment it was merged into the preview, and the app
   would then open a schema it does not know. The copy is the blast radius. Hot cues and
   playlists edited in the loop stay in the copy; that is the price, and it is why the
   rsync is re-run rather than the dirs being shared.

   **Stems are not in the copy.** The engine looks for stem bundles under
   `<data-dir>/state/stems` and `<data-dir>/state/stems-roformer-spike`
   (`apps/webui/server/stem_artifacts.py`). A store outside the copied data directory
   is not included by the rsync above. Configure the disposable loop to resolve
   the intended immutable bundle roots; private store census is not included here:

   ```bash
   S="$HOME/code/music-dj-tools/data/state"
   for n in stems stems-roformer-spike; do
     rmdir "$DST/state/$n" 2>/dev/null; ln -sfn "$S/$n" "$DST/state/$n"
   done
   ```

   The engine resolves the roots per request, so no restart is needed.

3. **Engine.** Start it through the loop launcher, which runs the preflight before it execs
   the worktree's own interpreter. Do not use `uv run`: macOS attributes permission prompts
   (media library, microphone) to the responsible process, and a grant to `uv` would cover
   every uv-run script on the machine (see issue #864):

   ```bash
   scripts/run_chrome_loop.sh --engine
   ```

4. **UI.** `cd apps/webui/frontend && pnpm exec vite dev --host 127.0.0.1`. Vite reads the
   registry pair and proxies `/api` to the backend port, so there is no CORS in play.

5. Open `http://127.0.0.1:<frontend-port>/performance` in Chrome. The footer build stamp
   shows the worktree's commit.

For Claude Code sessions in `afmac`, both processes exist as `preview_start` configs
(`opendj-chrome-engine`, `opendj-chrome-loop`) in that repo's `.claude/launch.json`.

Without Claude, `scripts/run_chrome_loop.sh` runs steps 3 and 4 (preflight first) with the
same commands, tails both logs, and stops both on Ctrl-C; `--engine` / `--vite` start one
side. It refuses to start over a port something else already holds, so a preview server
left running by a Claude session is reported by pid rather than fought over.

## The review loop end to end

The operator reviews the UI in one Chrome tab while changes are prepared in topic worktrees.

```
 YOU ── Chrome tab http://127.0.0.1:9448/performance ── review non-stop, drop comment pins
  │  pin: POST /api/v1/feedback (stored in the loop's copied library)     ▲
  │                                                                       │ vite HMR patches the module in place (no reload)
  ▼                                                                       │ or, if a reload is needed: 10 s countdown
┌─────────────────────────────────────────────────────────────────────┐  │ (REFRESH-01, vite-hold-full-reload.ts)
│ LOOP WORKTREE  ~/code/music-dj-tools-wt-p0-audio                     │──┘
│ branch chrome-loop-preview-live  =  origin/main + open PRs           │  throwaway: rebuilt, never pushed
│   vite dev :9448 ──/api──▶ engine :8728 (copied library, not yours) │
└─────────────────────────────────────────────────────────────────────┘
                 ▲ merge / cherry-pick the fix in (seconds)
                 │
┌─────────────────────────────────────────────────────────────────────┐
│ AGENT: read pins ─▶ triage ─▶ fix on a topic branch in its own       │
│        worktree (wt-fix-*, branch af--<thing>)                       │
└─────────────────────────────────────────────────────────────────────┘
```

Fix routing, by size:

```
 pin ──▶ quick UI fix (minutes) ──▶ topic branch ──▶ into the preview NOW ──▶ pin = fixed (green outline)
      │                                  │
      │                                  └──▶ batched into a "pin review" PR (#890 was the
      │                                       first) ──▶ queue:ready ──▶ merged to main by the
      │                                       agent or merge-fable ──▶ `just pin-merged <pr>`
      │                                       ──▶ pin = merged (green solid), archive / follow-on
      ├──▶ P0 (audio dead) ──▶ commit straight to main ──▶ preview rebuilt from main
      └──▶ bigger than a pin ──▶ GitHub issue (queue:ready + priority label) ──▶ pin = issued
                                 ──▶ nucbox fleet builds a PR ──▶ main ──▶ next preview rebuild
```

When main moves, the loop gets it at the next rebuild: reset to `origin/main`, re-merge the
still-open PRs, restart. `dev_loop_preflight` forces this by refusing to start a loop that
is behind main, and the shipped app's commit must be in the loop too (Chrome ahead of the
app is fine, behind is not). The rebuild is the one interruption: the countdown reload.

Pin markers (`FeedbackWidget.svelte`, #858):

| Marker | Status | Meaning |
|:---|:---|:---|
| solid amber | `open` | nobody has acted on it |
| solid amber + small oval at the bottom | `issued` | a GitHub issue was filed for it; the pin body links it |
| green outline | `fixed` | the fix is in the preview tab (not yet on main) |
| green solid | `merged` | the fix is on main; note carries the PR and sha |
| blue dot, top right | unread | an agent wrote to the pin since the maintainer last opened it |
| not drawn | `archived` | left the canvas via the archive button, history kept in the archive file |

There is no outlined-amber state. An outline around an amber pin is Chrome's focus ring on
the pin last clicked.

## What this loop can and cannot show

| Shows faithfully | Needs Safari on the same port, or the shipped app |
| :--- | :--- |
| Every component, route, store, transport rule, toast, watchdog | AudioWorklet cadence on a 512-frame Bluetooth device |
| The engine's routes, jobs, DB, analysis | The CLOCK badge under a real HAL stall; interrupted-context recovery |
| A fix's behaviour before it is committed | The DMG, updater (`UPDATE CHECK FAILED` is expected there, not in Chrome), signing, macOS permissions as the app sees them |

Chrome is the fast loop and the control arm; WebKit is the verdict. A Chrome green is never
the last thing run before a push (`.planning/TEST-LATENCY-REVIEW-19AUG.md`). Safari on the
same vite port is the cheap next rung: it is the system WebKit the shipped app embeds,
byte-identical on the probed capability surface (`docs/perf/research/webcodecs-availability.md`).

## What stops the loop falling behind the app

Nothing structural: the loop serves the worktree's commit, the DMG carries its own. The
`scripts/run_chrome_loop.sh --engine` entry point runs `python -m scripts.dev_loop_preflight`
first. It reads the shipped SHA
from the installed app's `payload/manifest.json` and refuses (exit 2) if that build carries
runtime changes (`apps/**`, `pyproject.toml`, `uv.lock`, `.python-version`, non-Markdown) the worktree lacks;
a tooling-only divergence (ship script, docs) is reported and allowed. It also refuses
(exit 3) if `origin/main` has commits the worktree lacks, because agents' merged fixes land
there; `--allow-behind-main` overrides that one. Ahead is always fine and is printed as
`+N`. The loop may be ahead of the app; runtime code behind the app is refused.

A loop that follows a shared preview branch
passes `--sync-ref <that ref>`, and the shipped-app check then judges it against
`/Applications/Open DJ (Preview).app` (`--preview-app`), the build it is a preview of. The plain
app moves with main, so judging a preview loop against it refused every boot once a newer main
DMG was installed (issue #5162). Loops on `origin/main` are still judged against the plain app.

## Do not

- Do not point the dev engine at the real `com.opendj.desktop` data dir while the shipped
  app is running: two writers on one `state.db`.
- Do not hot-swap the payload inside the installed `.app` to "skip the DMG": it breaks the
  code signature and the build stamp (DEVLOOP-04 in `specs/agent-control-and-redteam-fleet.md`).
- Do not treat `vite dev` as the production bundle for worklet questions: it serves packages
  untransformed (`playwright.webkit-deckload.config.ts`). `pnpm build --watch` served by the
  engine is the WebKit-accurate variant (DEVLOOP-03, #852).
