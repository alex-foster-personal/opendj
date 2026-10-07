feat(telemetry): full telemetry coverage with local-first metrics, template logs and a coverage ratchet (#4807)

Port of main-electron-rust PR #4807 onto main: `git cherry-pick -m 1 -x`
of its merge commit. Conflicts against main resolved by keeping main's
code and applying #4807's instrumentation on top:

- apps/analysis/pool.py, queue_runner.py: main's spawn_pool() and
  AnalysisProcessPoolExecutor kept (owner identity initargs, sentinel
  wakeup); the executor now subclasses InstrumentedProcessPool and
  spawn_pool takes the pool's metric name ("analysis-pool",
  "analysis-queue").
- apps/analysis/queue_targets.py: main's engine-or-ffmpeg duration probe
  kept, run in an InstrumentedThreadPool.
- apps/analysis/pcm_fingerprint.py, apps/vocals/cli.py: main's imports
  kept alongside spawn_thread.
- apps/audio-engine/src/serve.rs: main's metered progressive load and its
  panic message kept; the decoder thread is now the named
  "odj-decode" Builder thread with #4807's spawn-failure answer, and the
  control state carries both main's meters/load_paths and #4807's
  metrics/reply_errors.
- apps/engine_core/audio_engine.py: main's odj_audio_binary import kept
  with the metrics ingest and spawn_thread imports.
- Frontend (agent-orders, shell-commands, api-ingest, api-rb,
  api-track-reveal, ui-mirror, WaveRow, library-perf, app-init): main's
  newer logic kept (unreachable-engine catches, coverage timeout and
  cached query, leased mirror publisher, split-row waveform, fetch/paint
  stage decomposition, MIDI load reporter), with its raw fetches routed
  through timedFetch and #4807's metrics added.
- apps/fleet_mcp/__main__.py: deleted on main (moved to fleet-af), kept
  deleted.
- ci/test-scopes.yml: union of main's dependents and #4807's "shared".
- (private path): #4807's flag row added; its copy of
  the NAE-21 row dropped (main already has it).
- reqs.json: left at main's; regenerated in the follow-up commit that
  renumbers the requirement ids (OBS-07 already exists on main).

-Claude

(cherry picked from commit 89814bcb187935a003880672c37ba544dd879b55)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HV1zV5ARmnid5nuDsWF367

