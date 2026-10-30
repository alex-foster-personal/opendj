/**
 * AUTOPLAN-01..05: the real AutoPlay poll, decision, AutoPlay Next and IPC, with the engine's answers and the page's plan
 * runner as doubles. The deck starts holding the decided track, so the real load path only runs in the test that fails it.
 *
 * [if] the master is under its target [then] nothing is asked of the engine [⛔️ if decide is called].
 * [if] the master passes its target [then] one plan is scheduled for the decided window [⛔️ if none, or two].
 * [if] a decision is working [then] the old handoff waits [⛔️ if the follower starts playing].
 * [if] the plan fails [then] the phase is fallback with the reason, and the old handoff plays the cued track.
 * [if] AutoPlay goes off with the plan only scheduled [then] it is aborted [⛔️ if it stays armed].
 * [if] AutoPlay goes off with the plan running [then] it plays out [⛔️ if it is aborted].
 * [if] early planning is off [then] the end-of-track handoff alone runs [⛔️ if decide is called].
 * [if] Decide now is pressed before the target [then] the decision runs from the playhead.
 * [if] the follower is taken while the pair prepares [then] nothing is armed.
 * [if] an agent plan is scheduled [then] a decision neither starts nor loads [⛔️ if it touches a deck].
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { installTimerProbe, loadRuneModule } from './load-rune-module.mjs';

const ENTRY = [
	"export { installAutoPlay } from '$lib/rb/auto-play.svelte';",
	"export { autoPlayDecision, decideAutoPlayNow } from '$lib/rb/auto-play-early.svelte';",
	"export { autoPlayNextState, cancelAutoPlayNext, registerAutoPlayNextPlanRunner } from '$lib/rb/auto-play-next.svelte';",
	"export { registerAgentPlanStatus } from '$lib/rb/auto-play-plan-gate';",
	"export { uiPrefs } from '$lib/rb/prefs.svelte';",
	"export { setAutoPlayTrackFeed } from '$lib/rb/auto-play';",
	"export { deckStates } from '$lib/rb/audio-engine.svelte';"
].join('\n');

const WINDOW = { start_bar: 97, start_ms: 220_000, max_bars: 32, label: 'outro', energy: 0.4, vocal_free_bars: 32, score: 0.7, reason: 'r' };

function _shim() {
	const saved = new Map();
	const set = (key, value) => {
		saved.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
		Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
	};
	set('window', globalThis);
	set('location', { href: 'http://127.0.0.1:0/performance', pathname: '/performance' });
	set('navigator', { userAgent: 'auto-play-early-flow-test' });
	set('isSecureContext', true);
	return () => {
		for (const [key, descriptor] of saved) {
			if (descriptor === undefined) delete globalThis[key];
			else Object.defineProperty(globalThis, key, descriptor);
		}
	};
}

function _json(body, status = 200) {
	return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

const PLAN_ANSWER = {
	order: { plan: { id: 'plan-1' } },
	reason: 'chosen',
	intent: 'smooth',
	pick: { candidate: 'c', style: 'bass_swap_blend', bars: 16, score: 0.5, out_start_bar: 97, rationale: '' },
	out_bar_now: 80,
	considered: 1,
	rendered: 1,
	propose_ms: 0,
	render_ms: 0,
	scored: true,
	plan_ms: 1,
	saved: false,
	plan: { id: 'doc' }
};

async function until(condition, what, timeoutMs = 4000) {
	const deadline = Date.now() + timeoutMs;
	while (!condition()) {
		if (Date.now() > deadline) throw new Error(`timed out waiting for ${what}`);
		await new Promise((resolve) => setTimeout(resolve, 25));
	}
}

async function withSet(options, run) {
	const {
		position = 190_000,
		decide = { track: 'cued', window: WINDOW, style: 'long_blend', bars: 32, reason: 'decided' },
		early = true,
		prepareState = 'ready'
	} = options;
	const probe = installTimerProbe();
	const restore = _shim();
	const realFetch = globalThis.fetch;
	const realInfo = console.info;
	const infos = [];
	console.info = (...args) => infos.push(args.join(' '));
	const calls = { decide: [], plan: [], prepare: [] };
	globalThis.fetch = async (input, init) => {
		const url = String(typeof input === 'string' || input instanceof URL ? input : input.url);
		if (url.endsWith('/dj/transition/decide')) {
			calls.decide.push(JSON.parse(init.body));
			return _json(decide);
		}
		if (url.endsWith('/dj/transition/prepare')) {
			calls.prepare.push(JSON.parse(init.body));
			return _json({ state: prepareState, candidates: 1, reason: '', render_ms: 1, in_track: 'cued' });
		}
		if (url.endsWith('/dj/transition/plan')) {
			calls.plan.push(JSON.parse(init.body));
			return _json(PLAN_ANSWER);
		}
		return _json({ detail: 'unit runtime: no engine behind fetch' }, 503);
	};
	let uninstall = null;
	let mod = null;
	try {
		mod = await loadRuneModule(ENTRY);
		const runner = { current: null, scheduled: [], aborted: [] };
		mod.registerAutoPlayNextPlanRunner({
			schedule: async (plan) => {
				runner.scheduled.push(plan);
				runner.current = { id: String(plan.id), status: 'scheduled' };
				return { failed: null };
			},
			abort: async (id) => {
				runner.aborted.push(id);
				if (runner.current?.id === id) runner.current.status = 'aborted';
			},
			status: () => runner.current
		});
		mod.registerAgentPlanStatus(() => runner.current?.status ?? null);
		mod.uiPrefs.auto_play_enabled = true;
		mod.uiPrefs.auto_play_early_planning = early;
		uninstall = mod.installAutoPlay();
		mod.setAutoPlayTrackFeed('playlist-a', [
			{ stable_id: 'src-1', key: '8A', bpm: 124, file_exists: true },
			{ stable_id: 'next-2', key: '8A', bpm: 124, file_exists: true }
		]);
		Object.assign(mod.deckStates[2], { stable_id: 'cued', playing: false, anlz: { beatgrid: { beats: [] } }, duration_ms: 300_000 });
		Object.assign(mod.deckStates[1], {
			stable_id: 'src-1',
			playing: true,
			audible: true,
			is_master: true,
			duration_ms: 300_000,
			position_ms: position,
			key: '8A',
			bpm: 124
		});
		await run({ mod, runner, calls, infos });
	} finally {
		if (uninstall !== null) uninstall();
		// The plan watcher's interval outlives the page in this harness, and a live one keeps the runner open forever.
		await mod?.cancelAutoPlayNext();
		console.info = realInfo;
		globalThis.fetch = realFetch;
		restore();
		probe.restore();
	}
}

test('under the target the engine is not asked', async () => {
	await withSet({ position: 60_000 }, async ({ mod, calls }) => {
		await new Promise((resolve) => setTimeout(resolve, 600));
		assert.deepEqual(calls.decide, [], 'if AutoPlay decides before its target then broken');
		assert.equal(mod.autoPlayDecision.phase, 'idle');
		assert.equal(mod.autoPlayDecision.due_ms, 180_000, 'if the readout cannot say when it decides then broken');
	});
});

test('past the target one plan is scheduled for the decided window', async () => {
	await withSet({}, async ({ mod, runner, calls }) => {
		await until(() => mod.autoPlayDecision.phase === 'scheduled', 'the plan');
		await new Promise((resolve) => setTimeout(resolve, 400));
		assert.equal(calls.decide.length, 1, 'if AutoPlay decides twice for one track then broken');
		assert.deepEqual(calls.decide[0].shortlist, ['next-2'], "if AutoPlay's own pick is not the shortlist then broken");
		assert.equal(calls.decide[0].target_ms, 180_000);
		assert.equal(calls.decide[0].earliest_ms, 190_000 + 12_000, 'if a window it cannot reach is asked for then broken');
		assert.equal(runner.scheduled.length, 1);
		assert.equal(calls.plan[0].from_ms, WINDOW.start_ms, 'if the plan is not for the decided window then broken');
		assert.equal(mod.autoPlayDecision.lead_ms, WINDOW.start_ms - 190_000);
		assert.equal(mod.autoPlayDecision.bars, 16, 'if the readout keeps the provisional plan then broken');
		assert.equal(calls.prepare[0].from_ms, WINDOW.start_ms, 'if the decided window is not scored first then the M floor never sees it');
		assert.equal(mod.deckStates[2].playing, false, 'if the old handoff ran beside the plan then broken');
	});
});

test('a running plan moves the phase while AutoPlay yields, and done ends it', async () => {
	await withSet({}, async ({ mod, runner }) => {
		await until(() => mod.autoPlayDecision.phase === 'scheduled', 'the plan');
		runner.current.status = 'running';
		await until(() => mod.autoPlayDecision.phase === 'running', 'running');
		runner.current.status = 'done';
		await until(() => mod.autoPlayDecision.phase === 'done', 'done');
	});
});

test('a failed plan falls back with the reason and the old handoff keeps the cued track', async () => {
	await withSet({}, async ({ mod, runner, infos }) => {
		await until(() => mod.autoPlayDecision.phase === 'scheduled', 'the plan');
		runner.current.status = 'failed';
		await until(() => mod.autoPlayDecision.phase === 'fallback', 'the fallback');
		assert.match(mod.autoPlayDecision.reason, /ended failed/);
		assert.equal(mod.autoPlayDecision.track, 'cued', 'if the cued track is forgotten then the fallback picks again, broken');
		mod.deckStates[1].position_ms = 290_000;
		await until(() => infos.some((line) => line.includes('end-of-track handoff: the planned transition ended failed')), 'the log line');
	});
});

test('no answer from the engine falls back with its reason', async () => {
	await withSet({ decide: { track: null, reason: 'no phrase start left in the master' } }, async ({ mod, runner }) => {
		await until(() => mod.autoPlayDecision.phase === 'fallback', 'the fallback');
		assert.match(mod.autoPlayDecision.reason, /no phrase start/);
		assert.deepEqual(runner.scheduled, []);
	});
});

test('a seek back before the decision point gets a new decision, and past it does not', async () => {
	await withSet({ decide: { track: null, reason: 'nothing fits' } }, async ({ mod, calls }) => {
		await until(() => mod.autoPlayDecision.phase === 'fallback', 'the fallback');
		await new Promise((resolve) => setTimeout(resolve, 400));
		assert.equal(calls.decide.length, 1, 'if a fallback decides again on every poll then broken');
		mod.deckStates[1].position_ms = 60_000;
		await until(() => mod.autoPlayDecision.phase === 'idle', 'the reset');
		mod.deckStates[1].position_ms = 190_000;
		await until(() => calls.decide.length === 2, 'the second decision');
	});
});

test('a track that will not load falls back and forgets it', async () => {
	const decide = { track: 'never-loads', window: WINDOW, style: 'cut', bars: 4, reason: 'r' };
	await withSet({ decide }, async ({ mod, runner }) => {
		await until(() => mod.autoPlayDecision.phase === 'fallback', 'the fallback', 8000);
		assert.equal(mod.autoPlayDecision.track, null);
		assert.deepEqual(runner.scheduled, []);
	});
});

test('AutoPlay off aborts a plan that has not started', async () => {
	await withSet({}, async ({ mod, runner }) => {
		await until(() => mod.autoPlayDecision.phase === 'scheduled', 'the plan');
		mod.uiPrefs.auto_play_enabled = false;
		await until(() => runner.aborted.length === 1, 'the abort');
		assert.equal(mod.autoPlayDecision.phase, 'idle');
	});
});

test('AutoPlay off leaves a running plan to finish', async () => {
	await withSet({}, async ({ mod, runner }) => {
		await until(() => mod.autoPlayDecision.phase === 'scheduled', 'the plan');
		runner.current.status = 'running';
		await until(() => mod.autoPlayDecision.phase === 'running', 'running');
		mod.uiPrefs.auto_play_enabled = false;
		await new Promise((resolve) => setTimeout(resolve, 300));
		assert.deepEqual(runner.aborted, [], 'if switching off cuts a sounding transition then broken');
	});
});

test('early planning off leaves the end-of-track handoff alone', async () => {
	await withSet({ early: false, position: 290_000 }, async ({ calls, infos }) => {
		await until(() => infos.some((line) => line.includes('end-of-track handoff: early planning is off')), 'the log line');
		assert.deepEqual(calls.decide, [], 'if the flag does not restore the old path then broken');
	});
});

test('Decide now runs before the target, from the playhead', async () => {
	await withSet({ position: 60_000 }, async ({ mod, calls }) => {
		await mod.decideAutoPlayNow();
		await until(() => mod.autoPlayDecision.phase === 'scheduled', 'the plan');
		assert.equal(calls.decide[0].target_ms, 60_000, 'if the soft target still holds Decide now back then broken');
	});
});

test('Decide now is refused while a planned transition is running', async () => {
	await withSet({}, async ({ mod, runner }) => {
		await until(() => mod.autoPlayDecision.phase === 'scheduled', 'the plan');
		runner.current.status = 'running';
		await until(() => mod.autoPlayDecision.phase === 'running', 'running');
		await assert.rejects(mod.decideAutoPlayNow(), /running/);
	});
});

test('a follower taken while the pair prepares is never armed', async () => {
	await withSet({ prepareState: 'warming' }, async ({ mod, runner }) => {
		await until(() => mod.autoPlayDecision.phase === 'preparing', 'the prepare');
		mod.deckStates[2].stable_id = 'someone-elses';
		await until(() => mod.autoPlayDecision.phase === 'fallback', 'the fallback', 3000);
		assert.match(mod.autoPlayDecision.reason, /was taken/);
		assert.deepEqual(runner.scheduled, [], 'if a plan is armed onto a replaced deck then broken');
	});
});

test('the old handoff waits while a decision holds the follower', async () => {
	await withSet({ prepareState: 'warming' }, async ({ mod, infos }) => {
		await until(() => mod.autoPlayDecision.phase === 'preparing', 'the prepare');
		mod.deckStates[1].position_ms = 290_000;
		await new Promise((resolve) => setTimeout(resolve, 600));
		assert.equal(infos.filter((line) => line.includes('end-of-track handoff')).length, 0, 'if both own the follower then broken');
		mod.uiPrefs.auto_play_enabled = false;
	});
});

test('a plan an agent scheduled keeps the decision off the decks', async () => {
	await withSet({}, async ({ mod, runner, calls }) => {
		runner.current = { id: 'agent-plan', status: 'scheduled' };
		await new Promise((resolve) => setTimeout(resolve, 500));
		assert.deepEqual(calls.decide, [], 'if AutoPlay decides under another plan then broken');
		assert.equal(mod.autoPlayDecision.phase, 'idle');
	});
});
