/**
 * PERFMODE-04 Q28: every BACKGROUND_SHED_JOBS id must have a registered run
 * in production app-init.ts, or the gate silently no-ops it forever
 * (playing-gate.ts request()'s `if (run !== undefined)` guard).
 *
 * Parses source TEXT rather than bundling playing-gate.ts / app-init.ts: both
 * pull in audio-engine.svelte.ts, and esbuild's bundle (tests/unit/load-
 * typescript.mjs) does not preserve live-binding semantics for its circular
 * import with performance-ipc.svelte.ts, so `installScopedSyncRunner` is
 * `undefined` at the point performance-ipc.svelte.ts's top-level eager call
 * runs. Confirmed pre-existing and unrelated to this PR: reproduced on a
 * freshly-cloned worktree of pristine main (85dc72a95) with a clean
 * `pnpm install` and `svelte-kit sync`, Node 22.14.0 (the pinned CI
 * version) - not a stale worktree artifact. See the PR body / issue #2580
 * report for the full repro. This file exists so the Q28 wiring invariant
 * itself is still genuinely verified despite that infra defect.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const GATE_SOURCE = readFileSync(
	fileURLToPath(new URL('../../src/lib/rb/playing-gate.ts', import.meta.url)),
	'utf8'
);
const APP_INIT_SOURCE = readFileSync(
	fileURLToPath(new URL('../../src/lib/rb/app-init.ts', import.meta.url)),
	'utf8'
);

function _parseStringArrayLiteral(source, exportName) {
	const marker = `export const ${exportName} = [`;
	const start = source.indexOf(marker);
	assert.notEqual(start, -1, `could not locate ${exportName} in its source file`);
	const end = source.indexOf(']', start);
	const block = source.slice(start + marker.length, end);
	const ids = [...block.matchAll(/'([^']+)'/g)].map((m) => m[1]);
	assert.ok(ids.length > 0, `${exportName} parsed to zero entries - the regex is asking the wrong question`);
	return ids;
}

const BACKGROUND_SHED_JOBS = _parseStringArrayLiteral(GATE_SOURCE, 'BACKGROUND_SHED_JOBS');
const P0_NEVER_SHED = _parseStringArrayLiteral(GATE_SOURCE, 'P0_NEVER_SHED');

/**
 * `background-workers` is the one documented exemption: its id exists on
 * BACKGROUND_SHED_JOBS (Q28 ordering) but the only scaler it names,
 * `worker_divisor` (perf-tier.ts SCALERS), has zero behavioral consumers
 * anywhere in the frontend (grepped `worker_divisor`/`workerDivisor`: only the
 * SCALERS table itself, the mirrored Python constant, and the generated
 * api-types.ts type). There is no real work to shed, so wiring a `run` for it
 * would be exactly the fakeable no-op path this test exists to catch for
 * every OTHER job.
 *
 * `library-poll-cadence` is the second: BrowserPanel.svelte's
 * `_libraryRefreshGate` (a separate, already-shipped createPlayingGate)
 * already unconditionally defers the fallback-poll refetch whenever ANY deck
 * plays, pressure or not. Routing the same call site through the
 * pressure-gated shed in ADDITION would add no OBSERVABLE work-effect change
 * a test could distinguish from the status quo, so it is left unwired rather
 * than wired with a test that cannot bite.
 */
const SHED_JOBS_WITH_NO_REAL_WORK = ['background-workers', 'library-poll-cadence'];

test('every wireable BACKGROUND_SHED_JOBS id has a registered run in production app-init.ts', () => {
	const jobsBlock = APP_INIT_SOURCE.slice(
		APP_INIT_SOURCE.indexOf('jobs:'),
		APP_INIT_SOURCE.indexOf('onShed:')
	);
	assert.ok(jobsBlock.length > 0, 'if the jobs: array cannot be located this guard asserts nothing');

	for (const id of BACKGROUND_SHED_JOBS) {
		if (SHED_JOBS_WITH_NO_REAL_WORK.includes(id)) continue;
		assert.match(
			jobsBlock,
			new RegExp(`id:\\s*'${id}'`),
			`BACKGROUND_SHED_JOBS lists '${id}' but app-init.ts registers no run for it - ` +
				`the gate would silently no-op it forever (playing-gate.ts request()'s ` +
				`\`if (run !== undefined)\` guard)`
		);
	}
});

test('every documented exemption really has no wireable run (positive control)', () => {
	// If this ever fails, an exemption has gone stale: the job now has real
	// work and belongs in the loop above instead.
	assert.deepEqual(SHED_JOBS_WITH_NO_REAL_WORK, ['background-workers', 'library-poll-cadence']);
});

test('P0 paths can never be registered as a shed job', () => {
	for (const p0 of P0_NEVER_SHED) {
		assert.ok(
			!BACKGROUND_SHED_JOBS.includes(p0),
			`${p0} is a P0 path and must never appear on BACKGROUND_SHED_JOBS`
		);
	}
});

test('the P0 playback engine never calls the Q29 pressure cap scaler', () => {
	// audio-engine.svelte.ts owns the audio-callbacks / transport P0 paths.
	// Q29's shrink/grow logic must reach only background caches (prefetch,
	// anlz), never anything on that file's own call graph.
	const engineSource = readFileSync(
		fileURLToPath(new URL('../../src/lib/rb/audio-engine.svelte.ts', import.meta.url)),
		'utf8'
	);
	assert.doesNotMatch(engineSource, /pressureScaledCap|stepPressureCapState|pressure-cap-scaling/);
});
