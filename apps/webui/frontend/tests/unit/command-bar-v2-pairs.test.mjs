// requirement: CMDK-17
// requirement: CMDK-18
// requirement: CMDK-19
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * Cmd K slice 4 (#6092): the master deck's pairs on open, the Comet edge, and
 * an aligned pair loading at its captured offset with the fader at 0.
 *
 * Regression lines:
 * - if a partner is listed twice, or a self-edge counts as a pair, then broken
 * - if pairs are not the first group with nothing typed, or show while typing, then broken
 * - if a recent load that is also a pair is listed twice then broken
 * - if the aligned offset ignores the captured tempo ratio then broken
 * - if an aligned load can start before the fader is at 0 then broken
 * - if the offset is sent before the other commands, against a stale master position, then broken
 * - if the partner is left where the seek's own latency put it (measured 120 ms behind) then broken
 * - if an aligned seek is snapped to the partner's grid by quantize then broken
 * - if cmdk_pick reports an earlier pick's refusal as its own then broken
 * - if an aligned pick can lower a playing deck's fader nobody confirmed then broken
 * - if a dropped aligned pick leaves the deck's newer track muted then broken
 * - if a command after the load can reach a newer track, or a changed master, then broken
 * - if an aligned load starts playing while the master is stopped then broken
 * - if the master being off the captured overlap loads anyway then broken
 * - if the pulse is visible before or after its one lap then broken
 * - if a hover mid-lap restarts the pulse then broken
 * - if the ring can leave its row then broken
 * - if cmdk_pick takes a negative or fractional index then broken
 * - if a confirmed cmdk_pick replaces a track that landed from the deck's queue after it was sent then broken
 */

const SRC = new URL('../../src/', import.meta.url).pathname;
let pairs;
let edge;
let actions;
let ipc;
let load;
let picker;

before(async () => {
	pairs = await loadTypeScriptModule('src/lib/rb/cmdk-pairs.ts');
	edge = await loadTypeScriptModule('src/lib/rb/pair-edge.ts');
	actions = await loadTypeScriptModule('src/lib/rb/cmdk-actions.ts');
	ipc = await loadTypeScriptModule('tests/unit/fixtures/cmdk-ipc-entry.ts');
	load = await loadTypeScriptModule('src/lib/rb/cmdk-load.ts');
	picker = await loadTypeScriptModule('src/lib/rb/cmdk-deck-picker.ts');
});

const EDGE = (id, from, to, updated_at) => ({ pairing_id: id, from_stable_id: from, to_stable_id: to, updated_at });

test('CMDK-17: partners are each listed once, newest edit first, either direction, self-edges skipped', () => {
	const edges = [
		EDGE('p1', 'm', 'a', '2026-10-01T00:00:00Z'),
		EDGE('p2', 'b', 'm', '2026-10-03T00:00:00Z'),
		EDGE('p3', 'm', 'a', '2026-10-04T00:00:00Z'),
		EDGE('p4', 'm', 'm', '2026-10-05T00:00:00Z'),
		EDGE('p5', 'x', 'y', '2026-10-06T00:00:00Z'),
		EDGE('p6', 'c', 'm', '2026-10-02T00:00:00Z')
	];
	assert.deepEqual(pairs.partnersOf(edges, 'm'), ['a', 'b', 'c']);
	assert.deepEqual(pairs.partnersOf(edges, 'nobody'), []);
});

test('CMDK-17: with nothing typed the pairs come first and a paired recent load is not listed twice', () => {
	const base = {
		recent: [{ id: 'a' }, { id: 'r1' }, { id: 'r2' }],
		playlist: { rows: [{ id: 'pl' }], total: 1 },
		collection: { rows: [], total: 0 },
		commands: [],
		settings: [],
		pairs: [
			{ row: { id: 'a' }, aligned: true },
			{ row: { id: 'b' }, aligned: false }
		],
		idOf: (row) => row.id
	};
	const open = actions.layoutGroups({ ...base, query: '' });
	assert.deepEqual(
		open.groups.map((group) => group.id),
		['pairs', 'recent', 'playlist']
	);
	assert.deepEqual(
		open.groups[0].entries.map((entry) => [entry.row.id, entry.pair]),
		[
			['a', { aligned: true }],
			['b', { aligned: false }]
		]
	);
	assert.deepEqual(
		open.groups[1].entries.map((entry) => entry.row.id),
		['r1', 'r2']
	);
	// Typing replaces them with the search.
	const typed = actions.layoutGroups({ ...base, query: 'night' });
	assert.equal(
		typed.groups.some((group) => group.id === 'pairs'),
		false
	);
	// No pairs: the open view is what it was.
	const none = actions.layoutGroups({ ...base, query: '', pairs: [] });
	assert.deepEqual(
		none.groups.map((group) => group.id),
		['recent', 'playlist']
	);
	assert.equal(none.groups[0].entries.length, 3);
});

const SNAP = { aTempoRatio: 1.0, bTempoRatio: 1.02, aPositionMs: 60_000, bPositionMs: 30_000 };

test('CMDK-19: the aligned point follows the master at the captured rates', () => {
	const plan = pairs.alignedPlan(SNAP, { position_ms: 70_000, ratio: 1.0, playing: true }, 300_000);
	assert.equal(plan.ok, true);
	// 10 s on the master is 10.2 s on the partner at the captured rates.
	assert.equal(plan.position_ms, 40_200);
	assert.equal(plan.ratio, 1.02);
	assert.equal(plan.play, true);
	// The master has since sped up 3%: the partner keeps the captured ratio between them.
	const faster = pairs.alignedPlan(SNAP, { position_ms: 60_000, ratio: 1.03, playing: false }, 300_000);
	assert.equal(faster.position_ms, 30_000);
	assert.ok(Math.abs(faster.ratio - 1.02 * 1.03) < 1e-9);
	assert.equal(faster.play, false);
});

test('CMDK-19: an aligned load refuses, in words, when the master is off the overlap or the rates are unusable', () => {
	const early = pairs.alignedPlan(SNAP, { position_ms: 20_000, ratio: 1, playing: true }, 300_000);
	assert.equal(early.ok, false);
	assert.match(early.reason, /before the captured point/);
	const late = pairs.alignedPlan(SNAP, { position_ms: 400_000, ratio: 1, playing: true }, 300_000);
	assert.equal(late.ok, false);
	assert.match(late.reason, /past the end/);
	// Exactly at the end is off the track too; one ms before is not.
	const end = 30_000 + (300_000 - 30_000);
	assert.equal(pairs.alignedPlan({ ...SNAP, bTempoRatio: 1 }, { position_ms: 60_000 + end - 30_000, ratio: 1, playing: true }, 300_000).ok, false);
	assert.equal(pairs.alignedPlan({ ...SNAP, bTempoRatio: 1 }, { position_ms: 60_000 + end - 30_001, ratio: 1, playing: true }, 300_000).ok, true);
	// An unknown partner length refuses every point: the overlap's end cannot be checked (Sol P1).
	assert.equal(pairs.alignedPlan(SNAP, { position_ms: 70_000, ratio: 1, playing: true }, null).ok, false);
	assert.equal(pairs.alignedPlan(SNAP, { position_ms: 400_000, ratio: 1, playing: true }, null).ok, false);
	for (const bad of [{ ...SNAP, aTempoRatio: 0 }, { ...SNAP, bTempoRatio: -1 }, { ...SNAP, aTempoRatio: Number.NaN }]) {
		const out = pairs.alignedPlan(bad, { position_ms: 70_000, ratio: 1, playing: true }, 300_000);
		assert.equal(out.ok, false);
		assert.match(out.reason, /tempo is not usable/);
	}
	assert.equal(pairs.alignedPlan(SNAP, { position_ms: 70_000, ratio: 0, playing: true }, 300_000).ok, false);
	// Just before the partner's start is out; exactly at it is in.
	assert.equal(pairs.alignedPlan(SNAP, { position_ms: 30_000, ratio: 1, playing: true }, 300_000).ok, false);
	assert.equal(pairs.alignedPlan({ ...SNAP, bPositionMs: 0 }, { position_ms: 60_000, ratio: 1, playing: true }, 300_000).ok, true);
});

test('CMDK-19: the fader goes to 0 before the load; the rate after it, then the offset, and play (only with the master) last', () => {
	assert.deepEqual(pairs.beforeAlignedLoad(3), [{ type: 'fader', deck: 3, value: 0 }]);
	const plan = { ok: true, position_ms: 40_200, ratio: 1.02, play: true };
	// No play among the commands before the seek: the partner never starts from its load point (Codex r4276688934).
	// Beat Sync off first: play would otherwise re-phase the partner to the master's grid,
	// overwriting the captured rate and offset (Codex r4276826010).
	assert.deepEqual(pairs.afterAlignedLoad(3, plan), [
		{ type: 'beat_sync', deck: 3, enabled: false },
		{ type: 'tempo', deck: 3, ratio: 1.02 }
	]);
	assert.deepEqual(pairs.afterAlignedLoad(2, { ok: true, position_ms: 5, ratio: 1, play: false }), [
		{ type: 'beat_sync', deck: 2, enabled: false },
		{ type: 'tempo', deck: 2, ratio: 1 }
	]);
	assert.deepEqual(pairs.alignedSeek(3, plan), { type: 'seek', deck: 3, position_ms: 40_200, exact: true });
	assert.deepEqual(pairs.alignedPlay(3, plan), { type: 'play', deck: 3, playing: true });
	assert.equal(pairs.alignedPlay(2, { ok: true, position_ms: 5, ratio: 1, play: false }), null, 'a paused master leaves the partner paused');
});

test('CMDK-19: an aligned seek is exact: quantize never snaps the captured offset to the partner grid', () => {
	// The wire takes exact as an optional boolean and nothing else.
	assert.deepEqual(ipc.parsePerformanceCommandForTest({ type: 'seek', deck: 2, position_ms: 1_000, exact: true }), {
		type: 'seek',
		deck: 2,
		position_ms: 1_000,
		exact: true
	});
	// Control: a plain seek is unchanged, so every other caller still quantizes.
	assert.deepEqual(ipc.parsePerformanceCommandForTest({ type: 'seek', deck: 2, position_ms: 1_000 }), { type: 'seek', deck: 2, position_ms: 1_000 });
	assert.throws(() => ipc.parsePerformanceCommandForTest({ type: 'seek', deck: 2, position_ms: 1_000, exact: 'yes' }), /exact/);
	// The handler skips the grid only when asked.
	const src = readFileSync(`${SRC}lib/rb/performance-ipc.svelte.ts`, 'utf8');
	assert.match(src, /engine\.quantizedSeek\(command\.deck, command\.position_ms, command\.exact === true\)/);
	// Every seek the aligned load sends goes through alignedSeek, the first and each correction.
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const start = bar.indexOf('async function loadAligned');
	const end = bar.indexOf('function onWindowKeyup', start);
	assert.ok(start >= 0 && end > start, 'the aligned load is found and bounded');
	const body = bar.slice(start, end);
	assert.equal((body.match(/type: 'seek'/g) ?? []).length, 0, 'no hand-built seek that could miss exact');
	assert.equal((body.match(/send\(alignedSeek\(/g) ?? []).length, 2);
});

test('CMDK-19: an aligned pick refuses a playing deck nobody confirmed, before the fader moves', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const body = bar.slice(bar.indexOf('async function loadAligned'));
	const guard = body.indexOf('if (decks[deck].playing && !confirmed) {');
	const fader = body.indexOf('beforeAlignedLoad(deck)');
	assert.ok(guard > 0 && fader > guard, 'the playing check comes before the fader goes to 0');
	// Every way in says whether a playing deck was confirmed: Enter with no picker never is.
	assert.match(bar, /else void choose\(entry\.row, shift, entry\.pair, false\);/);
	assert.match(bar, /void choose\(p\.row, p\.vocalsOnly, p\.pair, confirmed\);/);
	assert.match(bar, /await choose\(entry\.row, vocalsOnly, entry\.pair, confirm\);/);
	assert.match(bar, /await loadAligned\(row, deck, confirmed, pair\.basis, vocalsOnly\);/);
});

test('CMDK-19: a dropped aligned pick gives the deck its fader back; later commands stop on a newer track or master', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const body = bar.slice(bar.indexOf('async function loadAligned'), bar.indexOf('function onWindowKeyup'));
	// Read in the deck's hold right before the 0, after rb-meta settles, so a fader move made while the
	// row settled is the value given back, not an older one (Codex r4279465781).
	const captured = body.indexOf('const shared = preludeBaseline.get(deck);');
	assert.ok(captured > body.indexOf('await settleForLoad('), 'the fader is read after the row settles');
	assert.ok(captured > body.indexOf('const fresh = pickLoad.resume(deck, start);'), 'and inside the hold, right before it goes to 0');
	const handler = body.slice(body.indexOf('} catch (exc) {'));
	assert.match(handler, /if \(\(landed === null \|\| decks\[deck\]\.load_generation !== landed\) && faderUntouched\(\) && !predecessorLanded\) \{\s*const back = await runCommand\(\{ type: 'fader', deck, value: priorFader \}\);/);
	// Only a fader this pick moved, and only while no fader write has been dispatched since: a
	// newer user, MIDI or IPC write is kept even at the same value or still in flight
	// (Codex r4276134970, r4276471326). The value is never the test.
	assert.match(body, /const before = faderWrites\(deck\);\s*const result = await run\(command\);\s*if \(!result\.ok\) throw new Error\(`deck \$\{deck\} fader did not go to 0 \(\$\{result\.reason\}\)`\);\s*if \(command\.type === 'fader'\) mine = faderWrites\(deck\) === before \+ 1 \? before \+ 1 : null;/);
	assert.match(body, /const faderUntouched = \(\): boolean => mine !== null && faderWrites\(deck\) === mine;/);
	assert.doesNotMatch(handler, /mixerState\.channels\[deck\]\.fader ===/);
	// Control: a track that did land keeps its fader at 0 (only a load that is not this pick's restores).
	assert.doesNotMatch(handler.slice(0, handler.indexOf('if ((landed === null')), /type: 'fader'/);
	const send = body.slice(body.indexOf('const send = async'), body.indexOf('try {'));
	// The track AND this pick's load: the same track loaded again by IPC, MIDI or AutoPlay
	// moves the load generation and is not this pair's to retune or seek (Codex r4275046306).
	assert.match(send, /const gone = lost\(\);\s*if \(gone !== null\) throw new Error\(gone\);/);
	const lost = body.slice(body.indexOf('const lost = (): string | null => {'), body.indexOf('const send = async'));
	assert.match(lost, /if \(decks\[deck\]\.stable_id !== row\.stable_id \|\| decks\[deck\]\.load_generation !== landed\) return `stopped: another load took deck \$\{deck\}`;/);
	assert.match(lost, /if \(masterDeck !== master\.deck \|\| decks\[master\.deck\]\.stable_id !== masterTrack \|\| decks\[master\.deck\]\.load_generation !== masterGeneration\) return 'loaded with the fader at 0, but stopped: the master changed';/);
	const tried = body.slice(body.indexOf('await pickLoad(settledRow, deck, start)'), body.indexOf('} catch (exc) {'));
	const pinned = tried.indexOf('landed = decks[deck].load_generation;');
	assert.ok(pinned > 0 && pinned < tried.indexOf('await send('), 'the load generation is pinned once the pick lands, before any follow-up');
	// Play waits on an untouched fader: a write during the load leaves the partner paused, never
	// heard from its load point before the seek (Codex r4276471318). Only play: a seek or tempo
	// still runs, so a deck already playing is never left off its offset.
	assert.match(send, /if \(command\.type === 'play' && !faderUntouched\(\)\) throw new Error\(`loaded, but left paused: deck \$\{deck\}'s fader moved during the load`\);/);
	assert.doesNotMatch(send, /command\.type === '(seek|tempo)' && !faderUntouched/);
	// Nothing after the load goes straight to runCommand.
	const afterLoad = body.slice(body.indexOf('await pickLoad(settledRow, deck, start)'), body.indexOf('} catch (exc) {'));
	assert.doesNotMatch(afterLoad, /runCommand\(/);
});

test('CMDK-19: cmdk_pick clears an earlier refusal before it picks, so a good pick never throws it', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const pick = bar.slice(bar.indexOf('target = id;\n\t\t\t\tpicker = null;'), bar.indexOf('snapshot: () => ({'));
	const clear = pick.indexOf('actionError = null;');
	const choose = pick.indexOf('const failed = await choose(entry.row, vocalsOnly, entry.pair, confirm);');
	const thrown = pick.indexOf('if (failed !== null) throw new Error(failed);');
	assert.ok(clear >= 0 && choose > clear && thrown > choose, 'cleared, then the pick, then its own result');
	// The pick's own result, never the bar's shared error (Codex r4278029276).
	assert.doesNotMatch(pick, /throw new Error\(actionError\)/);
});

test('CMDK-19: a corrective seek leads by seek latency in either direction, never by noise or a stall', () => {
	assert.equal(pairs.leadFor(1_000, 880), 120);
	assert.equal(pairs.leadFor(1_000, 1_030), -30);
	// Inside the floor: already aligned.
	assert.equal(pairs.leadFor(1_000, 1_000 - (pairs.SEEK_LEAD_FLOOR_MS - 1)), 0);
	assert.equal(pairs.leadFor(1_000, 1_000 - pairs.SEEK_LEAD_FLOOR_MS), 0, 'exactly the floor is aligned');
	assert.equal(pairs.leadFor(1_000, 1_000 - (pairs.SEEK_LEAD_FLOOR_MS + 1)), pairs.SEEK_LEAD_FLOOR_MS + 1, 'one ms past the floor is corrected');
	// Past the ceiling: the deck stalled or moved on, not latency.
	assert.equal(pairs.leadFor(5_000, 5_000 - pairs.SEEK_LEAD_CEIL_MS - 1), 0);
	assert.equal(pairs.leadFor(5_000, 5_000 - pairs.SEEK_LEAD_CEIL_MS), pairs.SEEK_LEAD_CEIL_MS);
	assert.equal(pairs.leadFor(0, 900), 0);
});

test('CMDK-19: the bar sends the fader before pickLoad and the rest after it, and refuses the master deck', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const body = bar.slice(bar.indexOf('async function loadAligned'));
	const fader = body.indexOf('beforeAlignedLoad(deck)');
	const load = body.indexOf('await pickLoad(settledRow, deck, start)');
	const after = body.indexOf('afterAlignedLoad(deck, plan)');
	const seek = body.indexOf('alignedSeek(deck, final)');
	const fresh = body.indexOf('const last = masterNow()');
	const play = body.indexOf('alignedPlay(deck, final)');
	assert.ok(fader > 0 && load > fader && after > load, 'fader 0, then load, then the rate');
	assert.ok(fresh > after && seek > fresh, 'the offset recomputed against the master after the rate');
	assert.ok(play > seek, 'play only once the paused partner sits on its offset (Codex r4276688934)');
	assert.match(body.slice(play), /const started = await send\(go\);/);
	// Then up to SEEK_CORRECTIONS seeks led by the accumulated lag, read after the state settles, only while playing.
	const loop = body.slice(play);
	assert.match(loop, /for \(let round = 0; round <= SEEK_CORRECTIONS && aligned !== null && aligned\.play; round\+\+\)/);
	assert.match(loop, /setTimeout\(resolve, SEEK_SETTLE_MS\)[\s\S]*const lag = [^\n]*leadFor\(want\.position_ms, decks\[deck\]\.position_ms\);[\s\S]*lead \+= lag;[\s\S]*want\.position_ms \+ lead/);
	assert.match(body, /if \(deck === master\.deck\)/);
	// choose routes an aligned pair there; a loose pair loads like any track.
	assert.match(bar, /if \(pair\?\.aligned === true\) return await loadAligned\(row, deck, confirmed, pair\.basis, vocalsOnly\);/);
	// Pairs are read only behind their own flag.
	assert.match(bar, /const pairsOn = \$derived\(actionsOn && cmdkPairsEnabled\(\)\)/);
	assert.match(bar, /if \(pairsOn\) requestCmdkPairs\(/);
});

test('CMDK-18: the Comet pulse runs one lap, fades in and out, and is invisible outside it', () => {
	const per = 400;
	for (const elapsed of [-1, -0.01, edge.COMET.lap * edge.LAP_END + 0.01, 10]) {
		for (const stroke of edge.pulseStrokes(elapsed, per)) assert.equal(stroke.opacity, 0, `elapsed ${elapsed}`);
	}
	const mid = edge.pulseStrokes(edge.COMET.lap / 2, per);
	assert.equal(mid.length, edge.TRAIL_PATHS + 1);
	assert.ok(Math.abs(mid[0].opacity - edge.COMET.bright) < 1e-9, 'the head peaks mid-lap');
	for (let k = 1; k < mid.length; k++) assert.ok(mid[k].opacity < mid[k - 1].opacity, 'each trail path is dimmer');
	assert.equal(mid[0].dasharray, `${per * edge.COMET.blen} ${per - per * edge.COMET.blen}`);
	assert.equal(Number(mid[0].dashoffset), -(0.5 * per));
	// Faded at both ends of the lap.
	assert.ok(edge.pulseStrokes(0.001, per)[0].opacity < 0.05);
	assert.ok(edge.pulseStrokes(edge.COMET.lap - 0.001, per)[0].opacity < 0.05);
});

test('CMDK-18: a lap draws until its trail ends, and a hover mid-lap does not restart it', () => {
	assert.equal(edge.lapRunning(null, 5), false);
	assert.equal(edge.lapRunning(1, 1 + edge.COMET.lap), true);
	assert.equal(edge.lapRunning(1, 1 + edge.COMET.lap * edge.LAP_END + 0.001), false);
	assert.equal(edge.mayReplay(null, 0), true);
	assert.equal(edge.mayReplay(1, 1 + edge.COMET.lap / 2), false);
	// The trail is still drawing just past the head's lap (Codex r4275264802), and is done just past LAP_END.
	assert.equal(edge.lapRunning(1, 1 + edge.COMET.lap + 0.01), true);
	assert.equal(edge.mayReplay(1, 1 + edge.COMET.lap + 0.01), false);
	assert.equal(edge.mayReplay(1, 1 + edge.COMET.lap * edge.LAP_END + 0.01), true);
});

test('CMDK-18: the ring stays inside its row, closes, and jags differently per seed', () => {
	const w = 300;
	const h = 40;
	const ring = edge.ringPath(w, h, 0.37, 3.3);
	assert.equal(ring.per, 2 * (w - 2 * edge.RING_INSET + h - 2 * edge.RING_INSET));
	assert.match(ring.d, /^M.*Z$/);
	const pts = ring.d.slice(1, -1).split('L').map((p) => p.split(',').map(Number));
	assert.ok(pts.length >= 24);
	for (const [x, y] of pts) {
		assert.ok(x >= 0 && x <= w && y >= 0 && y <= h, `${x},${y} inside ${w}x${h}`);
	}
	assert.notEqual(edge.ringPath(w, h, 0.37, 3.3).d, edge.ringPath(w, h, 0.37, 11).d);
	// Same inputs, same ring: nothing random.
	assert.equal(edge.ringPath(w, h, 0.37, 3.3).d, ring.d);
	assert.deepEqual(
		{ ...edge.COMET },
		{ amp: 0.4, swell: 0.3, waves: 2, wspeed: 3, lap: 0.85, blen: 0.07, fps: 16, base: 0.4, bright: 0.95 }
	);
});

test('CMDK-19: cmdk_pick reaches the mounted bar with its deck and confirm, and validates the index', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		await assert.rejects(ipc.dispatchPerformanceCommand({ type: 'cmdk_pick', index: 0 }), /requires the Cmd K v2 bar/);
		const picks = [];
		const unregister = ipc.registerCmdkAdapter({
			open: async () => {},
			close: () => {},
			snapshot: () => ({ open: true, query: '', selected: 0, rows: [] }),
			pick: async (index, deck, confirm, id, target, vocalsOnly) => {
				picks.push([index, deck, confirm, id, vocalsOnly]);
				if (index === 9) throw new Error('there is no row 9; the bar shows 2');
			}
		});
		await ipc.dispatchPerformanceCommand({ type: 'cmdk_pick', index: 1 });
		await ipc.dispatchPerformanceCommand({ type: 'cmdk_pick', index: 0, deck: 3, confirm: true });
		// The row id query().cmdk reported reaches the bar, so it can refuse a row that moved (Codex r4279465774).
		await ipc.dispatchPerformanceCommand({ type: 'cmdk_pick', index: 2, id: 'track-7' });
		// Shift+Enter reaches the bar through the same command as a plain pick (Codex P2 BLOCKING, #6691).
		await ipc.dispatchPerformanceCommand({ type: 'cmdk_pick', index: 0, vocals_only: true });
		assert.deepEqual(picks, [
			[1, null, false, null, false],
			[0, 3, true, null, false],
			[2, null, false, 'track-7', false],
			[0, null, false, null, true]
		]);
		await assert.rejects(ipc.dispatchPerformanceCommand({ type: 'cmdk_pick', index: 9 }), /no row 9/);
		for (const index of [-1, 1.5, '2', undefined]) {
			await assert.rejects(ipc.dispatchPerformanceCommand({ type: 'cmdk_pick', index }), /non-negative integer/);
		}
		await assert.rejects(ipc.dispatchPerformanceCommand({ type: 'cmdk_pick', index: 0, deck: 5 }));
		await assert.rejects(ipc.dispatchPerformanceCommand({ type: 'cmdk_pick', index: 0, confirm: 'yes' }));
		await assert.rejects(ipc.dispatchPerformanceCommand({ type: 'cmdk_pick', index: 0, extra: 1 }), /unexpected fields/);
		for (const id of ['', 7, null]) {
			await assert.rejects(ipc.dispatchPerformanceCommand({ type: 'cmdk_pick', index: 0, id }), /id must be a non-empty string/);
		}
		assert.equal(picks.length, 5);
		unregister();
	} finally {
		uninstall();
	}
});

test('CMDK-19: the bar pick applies the deck picker guard and surfaces its refusal', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const body = bar.slice(bar.indexOf('pick: async (index, deck, confirm, rowId, targetAt, vocalsOnly, source)'), bar.indexOf('snapshot: () => ({'));
	assert.match(body, /if \(!open\) throw/);
	// A pick pinned to a row id refuses an index that now holds another row, before anything is guarded
	// or loaded (Codex r4279465774).
	assert.match(body, /if \(rowId !== null && at !== rowId\) throw new Error/);
	assert.ok(body.indexOf('at !== rowId') < body.indexOf('selected = index'), 'the pin is checked before the row is selected');
	// The pin compares the same wire id the snapshot reports, so a pair row and the plain row of the
	// same track never match (Codex r4279717126).
	assert.match(body, /const at = cmdkWireId\(entry\);/);
	const snap = bar.slice(bar.indexOf('snapshot: () => ({'));
	assert.match(snap.slice(0, snap.indexOf("kind: entry.action.kind")), /id: cmdkWireId\(entry\),/);
	assert.match(body, /const refused = await pickGuard\(/);
	assert.match(body, /if \(refused !== null\) throw new Error\(refused\)/);
	assert.ok(body.indexOf('await pickGuard(') < body.indexOf('await choose('), 'the guard runs before the pick loads');
	assert.match(body, /await choose\(entry\.row, vocalsOnly, entry\.pair, confirm\)/);
	assert.match(body, /if \(failed !== null\) throw new Error\(failed\)/);
	// A load dispatched between the guard's read and the pick's start is refused, and nothing awaits
	// between that check and choose, which begins the pick synchronously (Codex r4278944613).
	const check = body.indexOf('if (deckLoadDispatches(id) !== seen.loads) throw');
	const pick = body.indexOf('await choose(');
	assert.ok(check > body.indexOf('await pickGuard(') && pick > check, 'the check sits between the guard and the pick');
	assert.ok(!body.slice(check, pick).includes('await '), 'no await between the check and the pick');
	assert.match(body, /confirm,\n\t+seen\n\t+\);/);
});

test('CMDK-19: the aligned steps run in one hold of both decks plus sync; a master seek waits for it (Codex r4275494643)', async () => {
	const { ScopedCommandScheduler, scopesOutsideHold } = await loadTypeScriptModule('src/lib/rb/performance-command-scheduler.ts');
	const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
	// Each step claims [deck, 'sync'] on its own: a master seek sent between two steps runs between them.
	const narrow = new ScopedCommandScheduler();
	const narrowOrder = [];
	await narrow.run([3, 'sync'], async () => narrowOrder.push('tempo'));
	const between = narrow.run([1, 'sync'], async () => narrowOrder.push('master seek'));
	await narrow.run([3, 'sync'], async () => narrowOrder.push('seek'));
	await between;
	assert.deepEqual(narrowOrder, ['tempo', 'master seek', 'seek'], 'control: without a hold the master moves between the steps');
	// One hold of [3, 1, sync]: steps sent from inside run at once; the master seek queues behind the whole hold.
	const scheduler = new ScopedCommandScheduler();
	const order = [];
	const hold = { scopes: new Set([3, 1, 'sync']), open: true };
	let seekQueued;
	const held = scheduler.run([...hold.scopes], async () => {
		assert.equal(scopesOutsideHold('tempo', [3, 'sync'], hold), null, 'a step the hold covers claims nothing more');
		order.push('tempo');
		seekQueued = scheduler.run([1, 'sync'], async () => order.push('master seek'));
		await tick();
		order.push('seek');
	});
	await held;
	hold.open = false;
	await seekQueued;
	assert.deepEqual(order, ['tempo', 'seek', 'master seek']);
	// A command the hold does not cover, or one sent once it ended, is refused, never run unscoped.
	assert.throws(() => scopesOutsideHold('fader', [2], { scopes: new Set([3, 1, 'sync']), open: true }), /needs 2/);
	assert.throws(() => scopesOutsideHold('seek', [3, 'sync'], hold), /already ended/);
	assert.deepEqual(scopesOutsideHold('seek', [3, 'sync'], undefined), [3, 'sync'], 'outside a hold a command claims its own scopes');
	// The bar sends everything after the load inside runCoordinated([deck, master.deck]); the page wires it.
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const aligned = bar.slice(bar.indexOf('async function loadAligned'), bar.indexOf('function onWindowKeyup'));
	const landed = aligned.indexOf('landed = decks[deck].load_generation;');
	const coordinated = aligned.indexOf('await runCoordinated([deck, master.deck], async (run) => {');
	assert.ok(landed > 0 && coordinated > landed, 'the hold starts once the load has landed, never around the load');
	assert.ok(aligned.indexOf('afterAlignedLoad(deck, plan)') > coordinated && aligned.indexOf('SEEK_CORRECTIONS') > coordinated);
	assert.match(aligned, /via = run;/);
	assert.match(aligned, /return await via\(command\);/);
	const page = readFileSync(`${SRC}routes/performance/+page.svelte`, 'utf8');
	assert.match(page, /runCoordinated=\{runCoordinatedFromUi\}/);
	const dispatcher = readFileSync(`${SRC}lib/rb/performance-ipc.svelte.ts`, 'utf8');
	assert.match(dispatcher, /const queueScopes = performanceCommandQueueScopes\(command\);\s*const scopes = scopesOutsideHold\(command\.type, queueScopes, hold\);/);
	assert.match(dispatcher, /await _commandScheduler\.run\(\[\.\.\.hold\.scopes\], \(\) =>\s*work\(\(command\) => runPerformanceCommandFromUi\(command, performance\.now\(\), hold\)\)/);
});

test('CMDK-19: the dispatcher counts every fader write it is sent, per deck, the same value too (Codex r4276471326)', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		const before = [1, 2, 3, 4].map((d) => ipc.deckFaderWriteCount(d));
		await ipc.dispatchPerformanceCommand({ type: 'fader', deck: 2, value: 0 }).catch(() => {});
		await ipc.dispatchPerformanceCommand({ type: 'fader', deck: 2, value: 0 }).catch(() => {});
		assert.equal(ipc.deckFaderWriteCount(2), before[1] + 2, 'a repeat of the same value is still a newer write');
		assert.equal(ipc.deckFaderWriteCount(1), before[0], 'another deck does not move');
		await ipc.dispatchPerformanceCommand({ type: 'trim', deck: 2, value: 0.5 }).catch(() => {});
		assert.equal(ipc.deckFaderWriteCount(2), before[1] + 2, 'another mixer control does not move it');
	} finally {
		uninstall();
	}
});

test('CMDK-19: an aligned load that lands after the bar closed and reopened leaves the new opening alone (Codex r4276688941)', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const openFn = bar.slice(bar.indexOf('async function openBar'), bar.indexOf('function closeBar'));
	assert.match(openFn, /barSession \+= 1;/);
	const body = bar.slice(bar.indexOf('async function loadAligned'), bar.indexOf('function onWindowKeyup'));
	assert.ok(body.indexOf('const session = barSession;') < body.indexOf('beforeAlignedLoad(deck)'), 'the opening is captured before any await');
	assert.match(body, /if \(open && barSession === session && pairLoads\.length === 0 && actionError === null\) closeBar\(\);/);
	assert.doesNotMatch(body, /\n\t+closeBar\(\);/);
});

test('CMDK-14: a guarded eject over IPC names the deck, not the guard object', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	assert.match(bar, /`deck \$\{outcome\.asked\} is playing; pick again with confirm to eject it`/);
	assert.doesNotMatch(bar, /`deck \$\{ejectGuard\} is playing/);
});

test('CMDK-19: the captured rate must fit the deck pitch range, checked before the fader moves (Codex r4276826024)', () => {
	assert.equal(pairs.ratioInRange(1.12, 16), true);
	assert.equal(pairs.ratioInRange(1.12, 8), false, 'a +-16% capture does not fit a +-8% deck');
	assert.equal(pairs.ratioInRange(0.92, 8), true, 'the edge of the range fits');
	assert.equal(pairs.ratioInRange(0.919, 8), false);
	assert.equal(pairs.ratioInRange(1.5, 100), true);
	assert.equal(pairs.ratioInRange(0, 100), false);
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const body = bar.slice(bar.indexOf('async function loadAligned'), bar.indexOf('function onWindowKeyup'));
	const check = body.indexOf('if (!ratioInRange(early.ratio, pitchRanges[deck])) {');
	assert.ok(check > 0, 'the bar checks the range');
	assert.ok(check < body.indexOf('beforeAlignedLoad(deck)'), 'before the fader moves');
	assert.match(body.slice(check, check + 400), /return fail\(/);
});

test('CMDK-19: every command after the load stays inside the hold of the partner, the master and sync (Beat Sync off included)', () => {
	const plan = { ok: true, position_ms: 40_200, ratio: 1.02, play: true };
	const sent = [...pairs.afterAlignedLoad(3, plan), pairs.alignedSeek(3, plan), pairs.alignedPlay(3, plan)];
	assert.ok(sent.some((c) => c.type === 'beat_sync'), 'the control: beat_sync is among them');
	const held = new Set([3, 1, 'sync']);
	for (const command of sent) {
		const scopes = ipc.performanceCommandQueueScopes(command);
		assert.ok(Array.isArray(scopes) && scopes.length > 0, `${command.type} has scopes`);
		for (const scope of scopes) assert.ok(held.has(scope), `${command.type} needs ${String(scope)}, outside the hold`);
	}
});

test('CMDK-19: the dispatcher counts a deck own-queue command when it is sent; loads, beat_sync and mixer writes do not move it (Codex r4277100142)', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		const count = () => [1, 2, 3, 4].map((d) => ipc.deckCommandDispatchCount(d));
		const start = count();
		for (const command of [
			{ type: 'play', deck: 2, playing: true },
			{ type: 'cue', deck: 2 },
			{ type: 'seek', deck: 2, position_ms: 1000 },
			{ type: 'tempo', deck: 2, ratio: 1 }
		]) {
			await ipc.dispatchPerformanceCommand(command).catch(() => {});
		}
		assert.equal(count()[1], start[1] + 4, 'play, cue, seek and tempo each count, whether or not they run');
		assert.equal(count()[0], start[0], 'another deck does not move');
		const mid = count()[1];
		const syncs = () => [1, 2, 3, 4].map((d) => ipc.deckCommandDispatchCount(d, 'beat_sync'));
		const syncStart = syncs();
		for (const command of [
			{ type: 'beat_sync', deck: 2, enabled: false },
			{ type: 'fader', deck: 2, value: 0 },
			{ type: 'eq', deck: 2, band: 'low', value: 0 },
			{ type: 'unload', deck: 2 }
		]) {
			await ipc.dispatchPerformanceCommand(command).catch(() => {});
		}
		assert.equal(count()[1], mid, 'beat_sync, mixer writes and unloads leave it alone');
		// beat_sync has its own count, moved only by beat_sync (Codex r4277769141).
		assert.equal(syncs()[1], syncStart[1] + 1, 'the beat_sync counts on its own');
		assert.equal(syncs()[0], syncStart[0], 'another deck does not move');
	} finally {
		uninstall();
	}
});

test('CMDK-19: the pair counts its own sends exactly as the dispatcher does, so only someone else stops it', () => {
	const plan = { ok: true, position_ms: 40_200, ratio: 1.02, play: true };
	const sent = [...pairs.afterAlignedLoad(3, plan), pairs.alignedSeek(3, plan), pairs.alignedPlay(3, plan)];
	for (const command of sent) {
		const counted = ipc.performanceCommandQueueScopes(command).includes(3) && !['load', 'unload', 'beat_sync'].includes(command.type);
		assert.equal(counted, command.type !== 'beat_sync', `${command.type}: the bar and the dispatcher agree`);
	}
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const body = bar.slice(bar.indexOf('async function loadAligned'), bar.indexOf('function onWindowKeyup'));
	assert.match(body, /if \(command\.type !== 'beat_sync'\) expected \+= 1;\s*else ownSync \+= 1;\s*return await via\(command\);/);
});

test('CMDK-19: a command sent to the partner during the load stops the pair before it acts (Codex r4277100142)', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const body = bar.slice(bar.indexOf('async function loadAligned'), bar.indexOf('function onWindowKeyup'));
	// The baseline is taken with the pick start, before the fader write and the load.
	const base = body.indexOf('let expected = start.used;');
	assert.ok(base > 0 && base < body.indexOf('beforeAlignedLoad(deck)'));
	assert.match(body, /const taken = \(\): boolean => deckCommands\(deck\) !== expected;/);
	// First thing inside the hold, so a play queued behind the load (run before the hold) is seen.
	assert.match(body, /const masterAt = deckCommands\(master\.deck\);\s*const syncAt = deckCommands\(deck, 'beat_sync'\);\s*await runCoordinated\(\[deck, master\.deck\], async \(run\) => \{\s*via = run;\s*if \(taken\(\)\) throw new Error\(/);
	// And before every send, so one sent mid-hold also stops it.
	assert.match(body, /if \(via === null\) throw new Error\([^\n]+\);\s*if \(taken\(\)\) throw new Error\(`stopped: deck \$\{deck\} was sent another command`\);/);
	const page = readFileSync(`${SRC}routes/performance/+page.svelte`, 'utf8');
	assert.match(page, /deckCommands=\{deckCommandDispatchCount\}/);
});

test('CMDK-19: corrective seeks wait with both decks released and stop once the deck is sent anything (Codex r4277100150)', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const body = bar.slice(bar.indexOf('async function loadAligned'), bar.indexOf('function onWindowKeyup'));
	const first = body.indexOf('await runCoordinated([deck, master.deck], async (run) => {');
	const firstEnd = body.indexOf('\t\t\t});\n', first);
	const sleep = body.indexOf('setTimeout(resolve, SEEK_SETTLE_MS)');
	const second = body.indexOf('await runCoordinated([deck, master.deck], async (run) => {', first + 1);
	assert.ok(first > 0 && firstEnd > first, 'the load steps hold');
	assert.ok(sleep > firstEnd, 'the settle wait is outside the first hold');
	assert.ok(second > sleep, 'each correction takes its own hold after the wait');
	const correction = body.slice(second, body.indexOf('if (done) break;'));
	assert.match(correction, /^await runCoordinated\(\[deck, master\.deck\], async \(run\) => \{\s*(\/\/[^\n]*\s*)*if \(taken\(\) \|\| !faderUntouched\(\) \|\| deckCommands\(master\.deck\) !== masterUsed \|\| deckCommands\(deck, 'beat_sync'\) !== syncUsed\) \{\s*done = true;\s*return;\s*\}/);
	assert.doesNotMatch(correction, /setTimeout/, 'no timer runs inside a correction hold');
});

test('CMDK-19: a failed aligned load does not report into a newer opening of the bar (Codex r4277100155)', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const body = bar.slice(bar.indexOf('async function loadAligned'), bar.indexOf('function onWindowKeyup'));
	assert.match(body, /const failure = `Deck \$\{deck\}: \$\{message\}`;\s*if \(barSession === session\) actionError = failure;\s*return failure;/);
	assert.doesNotMatch(body, /\n\t+actionError = `Deck \$\{deck\}: \$\{message\}`;/);
});

test('CMDK-19: an aligned pick lets the deck queue settle before it reads the deck or lowers its fader (Codex r4277285048)', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const body = bar.slice(bar.indexOf('async function loadAligned'), bar.indexOf('function onWindowKeyup'));
	const drain = body.indexOf('if (deckPending(deck) > 0) {');
	assert.ok(drain > 0, 'the drain is there');
	assert.ok(drain < body.indexOf('const master = masterNow();'), 'before the master and deck are read');
	assert.ok(drain < body.indexOf('if (decks[deck].playing && !confirmed)'), 'before the playing check');
	assert.ok(drain < body.indexOf('beforeAlignedLoad(deck)'), 'before the fader goes to 0');
	assert.match(body.slice(drain), /^if \(deckPending\(deck\) > 0\) \{\s*const drained = await waitDeckIdle\([\s\S]*?\);\s*if \(!drained\) \{\s*return fail\(`Deck \$\{deck\} is still busy; it was not loaded`\);/);
});

test('CMDK-19: an aligned pick settles rb-meta before its fader goes to 0, and lowers it in one scoped step with the freshness check (Codex r4278983114)', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const body = bar.slice(bar.indexOf('async function loadAligned'), bar.indexOf('function onWindowKeyup'));
	const settle = body.indexOf('const settledRow = await settleForLoad(row, fetchMeta);');
	const hold = body.indexOf('await runCoordinated([deck], async (run) => {');
	const fader = body.indexOf('beforeAlignedLoad(deck)');
	const load = body.indexOf('await pickLoad(settledRow, deck, start);');
	assert.ok(settle > 0 && settle < hold && hold < fader && fader < load, 'settle, then the hold, then the fader, then the load');
	// The metadata wait is gone from after the fader: the load gets the settled row.
	assert.doesNotMatch(body, /await pickLoad\(row, deck, start\)/);
	// Inside the hold, the freshness check comes before the fader and the fader goes through the hold's run.
	const inside = body.slice(hold, body.indexOf('});', fader));
	const fresh = inside.indexOf('const fresh = pickLoad.resume(deck, start);');
	assert.ok(fresh > 0 && fresh < inside.indexOf('beforeAlignedLoad(deck)'));
	assert.match(inside, /if \(fresh === null \|\| deckPending\(deck\) > 0\) throw new Error/);
	assert.match(inside, /const result = await run\(command\);/);
	assert.doesNotMatch(inside, /runCommand\(command\)/);
	// A play dispatched during the hold runs after it: checked at once, before any wait.
	const after = body.slice(body.indexOf('});', fader), load);
	assert.match(after, /if \(taken\(\) \|\| deckLoadDispatches\(deck\) !== start\.sent\) throw new Error/);
	assert.doesNotMatch(after, /await /);
});

test('CMDK-19: a readback past the seek-latency ceiling is a stall the bar reports, never taken as aligned (Codex r4277613903)', () => {
	// Control: seek latency and noise are not stalls.
	assert.equal(pairs.seekStalled(5_000, 5_000 - pairs.SEEK_LEAD_CEIL_MS), false);
	assert.equal(pairs.seekStalled(1_000, 1_000), false);
	assert.equal(pairs.seekStalled(1_000, 990), false);
	// Past the ceiling, either direction.
	assert.equal(pairs.seekStalled(5_000, 5_000 - pairs.SEEK_LEAD_CEIL_MS - 1), true);
	assert.equal(pairs.seekStalled(1_000, 1_600), true);
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const body = bar.slice(bar.indexOf('async function loadAligned'), bar.indexOf('function onWindowKeyup'));
	const loop = body.slice(body.indexOf('for (let round = 0; round <= SEEK_CORRECTIONS'));
	const stall = loop.indexOf('seekStalled(want.position_ms, decks[deck].position_ms)');
	assert.ok(stall > 0 && stall < loop.indexOf('const lag = '), 'checked before the lag reads it as nothing to correct');
	assert.match(loop.slice(stall), /^seekStalled\(want\.position_ms, decks\[deck\]\.position_ms\)\) \{\s*const off = [^\n]+;\s*throw new Error\(`loaded with the fader at 0, but not aligned: /);
});

test('CMDK-19: a command sent to the master during a correction wait ends the corrections (Codex r4277613896)', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const body = bar.slice(bar.indexOf('async function loadAligned'), bar.indexOf('function onWindowKeyup'));
	// The master's count is taken once the pair is aligned, inside the first hold.
	assert.match(body, /aligned = final;\s*(\/\/[^\n]*\s*)*masterUsed = masterAt;\s*(\/\/[^\n]*\s*)*syncUsed = syncAt \+ ownSync;\s*\}\);/);
	// Each correction checks it first thing in its hold, beside the partner's own.
	const loop = body.slice(body.indexOf('for (let round = 0; round <= SEEK_CORRECTIONS'));
	assert.match(loop, /await runCoordinated\(\[deck, master\.deck\], async \(run\) => \{\s*(\/\/[^\n]*\s*)*if \(taken\(\) \|\| !faderUntouched\(\) \|\| deckCommands\(master\.deck\) !== masterUsed \|\| deckCommands\(deck, 'beat_sync'\) !== syncUsed\) \{\s*done = true;\s*return;/);
});

test('CMDK-19: an aligned pick closes the bar only when the last overlapping aligned pick settles, and the IPC snapshot carries operation (Codex, #6691)', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const body = bar.slice(bar.indexOf('async function loadAligned'), bar.indexOf('function onWindowKeyup'));
	assert.match(body, /if \(open && barSession === session && pairLoads\.length === 0 && actionError === null\) closeBar\(\);/);
	const ipcTypes = readFileSync(`${SRC}lib/rb/performance-ipc-types.ts`, 'utf8');
	assert.match(
		ipcTypes,
		/export interface CmdkSnapshot \{[\s\S]*?operation\?:\s*(?:\n\s*)?\{[\s\S]*?status:\s*'loading'[\s\S]*?\}\s*\|\s*(?:\n\s*)?\{[\s\S]*?status:\s*'error'[\s\S]*?\};[\s\S]*?\n\}/
	);
});

test('CMDK-19: cmdk_pick rejects a plain pick that did not load, so an agent never reads a refused pick as done (Codex r4277653901)', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const pick = bar.slice(bar.indexOf('target = id;\n\t\t\t\tpicker = null;'), bar.indexOf('snapshot: () => ({'));
	assert.match(pick, /const failed = await choose\(entry\.row, vocalsOnly, entry\.pair, confirm\);\s*if \(failed !== null\) throw new Error\(failed\);/);
	const choose = bar.slice(bar.indexOf('async function choose('), bar.indexOf('/** The master as it plays now'));
	assert.match(choose, /Promise<string \| null>/);
	// A dropped or refused load resolves why; one that landed resolves null, soloed or not.
	assert.match(choose, /const dispatched = await pickLoad\(row, deck\);\s*(?:\/\/[^\n]*\s*)*if \(!dispatched\) return `deck \$\{deck\} did not load it \(a newer load won, or the load was refused\)`;\s*if \(!vocalsOnly \|\| decks\[deck\]\.stable_id !== row\.stable_id\) return null;/);
	assert.match(choose, /await soloVocals\(deck, settled\);\s*return null;\s*\}\s*$/);
	// The aligned pair returns its own result: a failure the bar no longer shows (a newer opening)
	// still reaches the caller (Codex r4278029276).
	assert.match(choose, /if \(pair\?\.aligned === true\) return await loadAligned\(row, deck, confirmed, pair\.basis, vocalsOnly\);/);
	assert.doesNotMatch(choose, /return actionError === null/);
	const body = bar.slice(bar.indexOf('async function loadAligned'), bar.indexOf('function onWindowKeyup'));
	assert.match(body, /^async function loadAligned\(row: CmdkRow, deck: DeckId, confirmed: boolean, basis: string \| undefined, vocalsOnly: boolean\): Promise<string \| null> \{/);
	assert.match(body, /if \(open && barSession === session && pairLoads\.length === 0 && actionError === null\) closeBar\(\);[\s\S]*?return null;/);
	// Every early refusal both shows and returns its message.
	assert.doesNotMatch(body.slice(0, body.indexOf('const session = barSession;')), /actionError = `/);
	assert.match(body, /const fail = \(message: string\): string => \{\s*if \(barSession === session\) actionError = message;\s*return message;\s*\};/);
});

test('CMDK-19: Beat Sync sent to the partner after its own Beat Sync off ends the corrections (Codex r4277769141)', async () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const body = bar.slice(bar.indexOf('async function loadAligned'), bar.indexOf('function onWindowKeyup'));
	// Baselined as the first hold is taken, plus the pair's own Beat Sync off: a Beat Sync another
	// sender dispatched while the hold ran is counted at dispatch and still ends the corrections
	// (Codex r4278029260).
	const first = body.slice(body.indexOf('const masterAt = deckCommands(master.deck);'));
	const at = first.indexOf("const syncAt = deckCommands(deck, 'beat_sync');");
	const own = first.indexOf('afterAlignedLoad(deck, plan)');
	const base = first.indexOf('syncUsed = syncAt + ownSync;');
	assert.ok(at > 0 && at < own && base > own && base < first.indexOf('\t\t\t});\n'), 'read as the hold is taken, set inside the first hold');
	// The dispatcher counts every beat_sync it is sent, apart from the deck-command count.
	const ipc = readFileSync(`${SRC}lib/rb/performance-ipc.svelte.ts`, 'utf8');
	assert.match(ipc, /if \(command\.type === 'beat_sync'\) _deckBeatSyncDispatches\[command\.deck\] \+= 1;/);
	assert.match(ipc, /return kind === 'beat_sync' \? _deckBeatSyncDispatches\[deck\] : _deckCommandDispatches\[deck\];/);
});

test('CMDK-19: the last corrective seek is verified; a pair still off after it is reported, never closed as landed (Codex r4277769146)', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const body = bar.slice(bar.indexOf('async function loadAligned'), bar.indexOf('function onWindowKeyup'));
	const loop = body.slice(body.indexOf('for (let round = 0; round <= SEEK_CORRECTIONS'));
	// One readback per correction plus one more for the last, each after the settle wait.
	assert.match(loop, /^for \(let round = 0; round <= SEEK_CORRECTIONS && aligned !== null && aligned\.play; round\+\+\) \{\s*await new Promise\(\(resolve\) => setTimeout\(resolve, SEEK_SETTLE_MS\)\);/);
	// On that final readback a lag past the floor throws rather than seeking again.
	assert.match(loop, /if \(lag === 0\) \{\s*done = true;\s*\} else if \(round === SEEK_CORRECTIONS\) \{\s*throw new Error\(`loaded with the fader at 0, but not aligned: it is still/);
	// A plan the master's clock has made invalid is reported with its reason, never read as zero lag (Codex r4278201290).
	assert.match(loop, /if \(want === null\) throw new Error\([^\n]*\);\s*if \(!want\.ok\) throw new Error\(`loaded with the fader at 0, but not aligned: \$\{want\.reason\}`\);\s*const lag = leadFor\(want\.position_ms, decks\[deck\]\.position_ms\);\s*if \(lag === 0\) \{\s*done = true;/);
	assert.doesNotMatch(loop, /!want\.ok \? 0|!want\.ok \|\| lag === 0/);
	// The nudge still only runs on the correction rounds.
	assert.ok(loop.indexOf('round === SEEK_CORRECTIONS') < loop.indexOf('const nudge = await send('));
});

test('CMDK-19: a master tempo change that pushes the captured rate out of range holds the load back, or stops before the tempo (Codex r4277873847)', async () => {
	// The guard asks fits() with the rest, right before the load is sent.
	const row = { stable_id: 'cmdk-pair', file_exists: true, file_availability: 'present' };
	const state = { fits: true, sent: [] };
	const pick = load.createPickLoader(
		async () => assert.fail('a settled row asks rb-meta nothing'),
		async (picked, deck, stillNewest) => {
			if (!stillNewest(0)) return false;
			state.sent.push(picked.stable_id);
			return true;
		},
		() => 'old|1|0|paused',
		// The dispatcher counts each load as it is sent.
		{ loadsDispatched: () => state.sent.length, commandsDispatched: () => 0, pending: () => 0, idle: async () => true }
	);
	// Control: still fits, the load goes out.
	assert.equal(await pick(row, 3, { ...pick.begin(3), fits: () => state.fits }), true);
	// No longer fits: held back, the deck keeps what it has.
	state.fits = false;
	assert.equal(await pick(row, 3, { ...pick.begin(3), fits: () => state.fits }), false);
	assert.deepEqual(state.sent, ['cmdk-pair']);
	// A start without fits loads as before.
	assert.equal(await pick(row, 3, pick.begin(3)), true);
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const body = bar.slice(bar.indexOf('async function loadAligned'), bar.indexOf('function onWindowKeyup'));
	assert.match(body, /fits: \(\) => \{\s*holdReason = faderUntouched\(\) \? alignRefusal\(masterNow\(\)\) : [^;]+;\s*return holdReason === null;\s*\}\s*\};\s*holdReason = null;\s*const dispatched = await pickLoad\(settledRow, deck, start\);\s*if \(!dispatched \|\| decks\[deck\]\.stable_id !== row\.stable_id\) \{\s*if \(holdReason !== null\) throw new Error\(`Not loaded: \$\{holdReason\}`\);/);
	// The same master deck AND track, and a plan inside the captured overlap: a stale alignment
	// holds the load back rather than replacing the deck's track (Codex r4278029270).
	assert.match(body, /const alignRefusal = \(now: ReturnType<typeof masterNow>\): string \| null => \{\s*if \(now === null\) return 'there is no master deck any more';\s*if \(now\.deck !== master\.deck \|\| decks\[master\.deck\]\.stable_id !== masterTrack \|\| decks\[master\.deck\]\.load_generation !== masterGeneration\) return 'the master changed';\s*const plan = alignedPlan\(snap, now, row\.duration_ms \?\? null\);\s*if \(!plan\.ok\) return plan\.reason;\s*if \(!ratioInRange\(plan\.ratio, pitchRanges\[deck\]\)\)/);
	// After the load, the recomputed rate is checked before the tempo goes out.
	const hold = body.slice(body.indexOf('await runCoordinated([deck, master.deck], async (run) => {'));
	const check = hold.indexOf('if (!ratioInRange(plan.ratio, pitchRanges[deck])) {');
	assert.ok(check > 0 && check < hold.indexOf('for (const command of afterAlignedLoad(deck, plan))'));
});

test('CMDK-19: each correction checks both decks still hold what the pair aligned, even one that ends without a seek (Codex r4277873853)', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const body = bar.slice(bar.indexOf('async function loadAligned'), bar.indexOf('function onWindowKeyup'));
	const loop = body.slice(body.indexOf('for (let round = 0; round <= SEEK_CORRECTIONS'));
	const gone = loop.indexOf('const gone = lost();');
	assert.ok(gone > 0 && gone < loop.indexOf('const now2 = masterNow();'), 'before the readback and any done path');
	assert.match(loop.slice(gone), /^const gone = lost\(\);\s*if \(gone !== null\) throw new Error\(gone\);/);
});

test('CMDK-19: rows that arrive after the bar opened keep the highlight on its entry (Codex r4277873858)', () => {
	const t = (id, pair) => pairs.cmdkEntryKey({ kind: 'track', row: { stable_id: id }, ...(pair ? { pair: { aligned: true } } : {}) });
	assert.notEqual(t('a', true), t('a', false), 'a pair row is not the same entry as the track elsewhere');
	assert.equal(pairs.cmdkEntryKey({ kind: 'action', action: { id: 'eject' } }), 'a:eject');
	// The pairs group lands above the highlighted playlist row: it follows its row.
	const before = [t('x'), t('y'), t('z')];
	const after = [t('p1', true), t('p2', true), ...before];
	assert.equal(pairs.reanchorSelection(before[1], after, 1), 3);
	// Control: an entry that is gone leaves the highlight where it was; no anchor changes nothing.
	assert.equal(pairs.reanchorSelection(t('gone'), after, 1), 1);
	assert.equal(pairs.reanchorSelection(null, after, 2), 2);
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	// Only while the query is unchanged: typing still puts the highlight back on the first row.
	assert.match(bar, /if \(list !== anchoredList && query === anchoredQuery\) \{\s*const moved = reanchorSelection\(anchor, list\.map\(flatEntryKey\), at\);/);
	assert.match(bar, /anchor = list\[at\] === undefined \? null : flatEntryKey\(list\[at\]\);/);
	// CMDK-6848: a playlist row keys by its id, every other row as before.
	assert.match(bar, /if \(entry\.kind === 'list'\) return `pl:\$\{entry\.list\.playlist_id\}`;\s*return cmdkEntryKey\(entry\);/);
});

test('CMDK-16: the deck picker lets a queued load land before it asks to replace a playing deck (Codex r4278029265)', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const fn = bar.slice(bar.indexOf('function pickerLoad('), bar.indexOf('async function settledGuard('));
	// With loads still queued, the guard is the settled one, set only while this picker is still open.
	assert.match(fn, /if \(action === 'ask'\) \{\s*(\/\/[^\n]*\s*)*if \(deckPending\(deck\.id\) > 0\) \{\s*const session = barSession;\s*void settledGuard\(deck\.id, session\)\.then\(\(guard\) => \{\s*if \(guard !== null && picker === p && barSession === session\) p\.guard = guard;\s*\}\);\s*return;\s*\}/);
	// Control: an idle deck still asks at once, about what it shows.
	assert.match(fn, /return;\s*\}\s*p\.guard = replaceGuard\(deck\.id, decks\[deck\.id\], deckLoadDispatches\(deck\.id\), deckCommands\(deck\.id\)\);\s*return;/);
	// settledGuard drains first and refuses a deck that never settles.
	const settled = bar.slice(bar.indexOf('async function settledGuard('), bar.indexOf('async function confirmGuard('));
	assert.ok(settled.indexOf('await waitDeckIdle(') < settled.indexOf('return replaceGuard('));
});

/** A deck the guard reads, with a queue whose next drain may land `next` on it. */
function guardDeck({ playing = true, is_master = false, queued = 0, next = null, drains = true } = {}) {
	const state = { id: 2, title: 'A', artist: null, loaded: true, playing, is_master, stable_id: 'a', load_generation: 1 };
	let loads = 4 + queued;
	return {
		ports: {
			deck: () => ({ ...state }),
			loads: () => loads,
			commands: () => 0,
			pending: () => queued,
			idle: async () => {
				if (!drains) return false;
				if (next !== null) Object.assign(state, next);
				queued = 0;
				return true;
			}
		},
		dispatch: () => (loads += 1)
	};
}

test('CMDK-19: a confirmed cmdk_pick replaces only the load it was sent against, never one that landed from the queue (Codex r4278581453)', async () => {
	// Control: nothing queued, a confirmed pick of a playing deck goes ahead.
	assert.equal(await picker.pickGuard(2, guardDeck().ports, true), null);
	// Not confirmed, a playing deck asks.
	assert.match(await picker.pickGuard(2, guardDeck().ports, false), /playing; pick again with confirm/);
	// A queued load lands a new track on the playing deck: the confirm was not for it.
	const landed = guardDeck({ queued: 1, next: { stable_id: 'b', load_generation: 2 } });
	assert.match(await picker.pickGuard(2, landed.ports, true), /took another load before this pick/);
	// The same track loaded again (a new generation) is another load too.
	const reloaded = guardDeck({ queued: 1, next: { load_generation: 2 } });
	assert.match(await picker.pickGuard(2, reloaded.ports, true), /took another load/);
	// A queued load lands a paused track on the deck that was playing: still not the one confirmed.
	const paused = guardDeck({ queued: 1, next: { stable_id: 'b', load_generation: 2, playing: false } });
	assert.match(await picker.pickGuard(2, paused.ports, true), /took another load/);
	// A confirm sent against a stopped deck pins its paused load too: a queued load that lands on it
	// before the pick is another track nobody confirmed (CMDK-19; Sol P1 on 74e7d33cb).
	const stoppedConfirm = guardDeck({ playing: false, queued: 1, next: { stable_id: 'b', load_generation: 2 } });
	assert.match(await picker.pickGuard(2, stoppedConfirm.ports, true), /took another load before this pick/);
	// Control: the same paused deck with nothing landed from the queue still confirms.
	assert.equal(await picker.pickGuard(2, guardDeck({ playing: false }).ports, true), null);
	// A queued play (no load) leaves the confirm standing.
	assert.equal(await picker.pickGuard(2, guardDeck({ queued: 1 }).ports, true), null);
	// A queued load onto a stopped deck needs no confirm, so the pick goes ahead (the overshoot control).
	const stopped = guardDeck({ playing: false, queued: 1, next: { stable_id: 'b', load_generation: 2 } });
	assert.equal(await picker.pickGuard(2, stopped.ports, false), null);
	// A queued play that starts the deck asks first, read after the queue landed.
	assert.match(await picker.pickGuard(2, guardDeck({ playing: false, queued: 1, next: { playing: true } }).ports, false), /playing; pick again with confirm/);
	// A queue that never drains is busy, not a verdict on the old track.
	assert.match(await picker.pickGuard(2, guardDeck({ queued: 1, drains: false }).ports, true), /still busy/);
	// The master is refused whatever the confirm.
	assert.match(await picker.pickGuard(2, guardDeck({ is_master: true }).ports, true), /is the master/);
	// A load dispatched to the deck after the pick was sent but before its guard read is not the one confirmed either.
	const raced = guardDeck({ queued: 1 });
	const idle = raced.ports.idle;
	raced.ports.idle = async () => {
		raced.dispatch();
		return idle();
	};
	assert.match(await picker.pickGuard(2, raced.ports, true), /took another load/);
	// The guard reports the load count its verdict read, after the queue landed, for the caller to pin.
	const counted = guardDeck({ queued: 1 });
	const seen = { loads: -1 };
	assert.equal(await picker.pickGuard(2, counted.ports, true, seen), null);
	assert.equal(seen.loads, counted.ports.loads());
	counted.dispatch();
	assert.notEqual(seen.loads, counted.ports.loads(), 'a load dispatched after the verdict shows against it');
});

test('CMDK-19: a pair row has its own wire id, apart from the same track as a plain row (Codex r4279717126)', () => {
	const row = { stable_id: 'trk-1' };
	assert.equal(pairs.cmdkWireId({ kind: 'track', row }), 'trk-1', 'a plain row keeps its stable_id');
	assert.equal(pairs.cmdkWireId({ kind: 'track', row, pair: { aligned: false } }), 'pair:trk-1:loose', 'loose pairs too, in their own mode');
	assert.notEqual(pairs.cmdkWireId({ kind: 'track', row }), pairs.cmdkWireId({ kind: 'track', row, pair: { aligned: true } }));
	assert.equal(pairs.cmdkWireId({ kind: 'action', action: { id: 'eject' } }), 'eject', 'settings and commands keep their ids');
});

test('CMDK-19: an aligned pair id names the master and snapshot it was read against (Codex P2 BLOCKING, #6691)', () => {
	const row = { stable_id: 'trk-1' };
	const snap = { aTempoRatio: 1, bTempoRatio: 1.02, aPositionMs: 1000, bPositionMs: 2000 };
	const read = pairs.pairBasis('master-a', 1, snap);
	const at = (basis) => pairs.cmdkWireId({ kind: 'track', row, pair: { aligned: true, basis } });
	assert.equal(at(read), at(pairs.pairBasis('master-a', 1, snap)), 'the same master and snapshot keep the id');
	assert.notEqual(at(read), at(pairs.pairBasis('master-b', 1, snap)), 'a master that moved refuses the pick read under the old one');
	assert.notEqual(at(read), at(pairs.pairBasis('master-a', 1, { ...snap, bPositionMs: 2500 })), 'a recaptured snapshot refuses it too');
});

test('CMDK-15/17: query().cmdk reports the master pairs lookup, so a failed read is not read as no pairs (Codex r4279717136)', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const snap = bar.slice(bar.indexOf('snapshot: () => ({'));
	const pairsPart = snap.slice(0, snap.indexOf('})\n\t\t})'));
	assert.match(pairsPart, /\.\.\.\(pairsOn && masterDeck !== null/);
	assert.match(pairsPart, /pairsState\.status === 'error'\s*\?\s*\{ status: 'error' as const, message: pairsState\.message \}/);
	// Ready only for the track the master holds now, the same test pairRows applies; anything else is still loading.
	assert.match(pairsPart, /pairsState\.status === 'ready' && pairsState\.master === decks\[masterDeck\]\.stable_id \? \('ready' as const\) : \('loading' as const\)/);
	for (const f of ['lib/rb/cmdk-actions.ts', 'lib/rb/performance-ipc-types.ts']) {
		assert.match(readFileSync(`${SRC}${f}`, 'utf8'), /pairs\?: \{ status: 'loading' \| 'ready' \} \| \{ status: 'error'; message: string \};/, f);
	}
});

test('CMDK-15/19: query().cmdk reports the collection load, so a cold or failed index is not read as no matches (Codex r4280115661)', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const snap = bar.slice(bar.indexOf('snapshot: () => ({'));
	const part = snap.slice(0, snap.indexOf('})\n\t\t})'));
	assert.match(part, /\.\.\.\(open\s*\?\s*\{\s*collection:/);
	assert.match(part, /collectionError !== null\s*\?\s*\{ status: 'error' as const, message: collectionError \}/);
	// Loading until the index is current and the search over it is warm: the same condition the bar's own "Loading the collection" uses.
	assert.match(part, /collectionPending \|\| !searchWarm \? \('loading' as const\) : \('ready' as const\)/);
	assert.match(bar, /\(collectionPending \|\| collectionError !== null \|\| !searchWarm\) && tokens\.length > 0 && trackSearch/);
	for (const f of ['lib/rb/cmdk-actions.ts', 'lib/rb/performance-ipc-types.ts']) {
		assert.match(readFileSync(`${SRC}${f}`, 'utf8'), /collection\?: \{ status: 'loading' \| 'ready' \} \| \{ status: 'error'; message: string \};/, f);
	}
});

test('CMDK-19: a fader write during the aligned prelude stops the pick before the load (Codex r4280152479)', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const body = bar.slice(bar.indexOf('async function loadAligned'), bar.indexOf('function onWindowKeyup'));
	const guard = body.indexOf("if (!faderUntouched()) throw new Error(`deck ${deck}'s fader moved before the pair loaded; it was not loaded`);");
	assert.ok(guard > 0, 'the prelude checks the fader is still this pick\'s 0');
	// After the prelude's hold set mine, and before the load goes out.
	assert.ok(guard > body.indexOf('if (command.type === \'fader\') mine ='), 'after the 0 is recorded');
	assert.ok(guard < body.indexOf('await pickLoad(settledRow, deck, start)'), 'before the load');
	// The give-back runs only on an untouched fader, so the newer write that stopped the pick stays.
	const handler = body.slice(body.indexOf('} catch (exc) {'));
	assert.match(handler, /&& faderUntouched\(\) && !predecessorLanded\) \{\s*const back = await runCommand/);
});

test('CMDK-19: a fader write during the rb-meta wait holds the load at its last-moment guard (Codex r4280328376)', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const body = bar.slice(bar.indexOf('async function loadAligned'), bar.indexOf('function onWindowKeyup'));
	// pickLoad checks fits() after its own awaits, right before the load goes out: the fader is part of it.
	const fits = body.indexOf('holdReason = faderUntouched() ? alignRefusal(masterNow()) :');
	assert.ok(fits > 0, 'the load guard requires the fader still at this pick\'s 0');
	assert.ok(fits < body.indexOf('await pickLoad(settledRow, deck, start)'), 'set before the load');
});

test('CMDK-19: a fader write after play ends the corrective seeks before the next one (Codex r4280308822)', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const body = bar.slice(bar.indexOf('async function loadAligned'), bar.indexOf('function onWindowKeyup'));
	const loop = body.slice(body.indexOf('for (let round = 0; round <= SEEK_CORRECTIONS'), body.indexOf('pairLoads = pairLoads.filter'));
	// Checked inside each correction's hold, ahead of its seek: a write while play settled or during the
	// settle wait stops the corrections, so the partner is never seeked while it may be audible.
	const stop = loop.indexOf('if (taken() || !faderUntouched() ||');
	assert.ok(stop > 0, 'each round checks the fader is still the pick\'s 0');
	assert.ok(stop < loop.indexOf('await send(alignedSeek('), 'before the corrective seek');
	assert.match(loop.slice(stop, stop + 200), /\) \{\s*done = true;/);
});

test('CMDK-19: overlapping aligned picks share the deck fader baseline, so the last one to fail gives back the deck\'s own fader (Codex P2 BLOCKING, #6691)', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const body = bar.slice(bar.indexOf('async function loadAligned'), bar.indexOf('function onWindowKeyup'));
	// The baseline is read from the shared record while another pick holds the deck's 0, not from the live fader.
	assert.match(body, /const shared = preludeBaseline\.get\(deck\);\s*priorFader = shared\?\.fader \?\? mixerState\.channels\[deck\]\.fader;/);
	assert.match(body, /preludeBaseline\.set\(deck, \{ fader: priorFader, picks: \(shared\?\.picks \?\? 0\) \+ 1 \}\);/);
	// Released in every exit, so a deck with no pick in flight has no stale baseline.
	assert.match(body, /\} finally \{[\s\S]*if \(inPrelude\) \{/);
	assert.match(bar, /const preludeBaseline = new Map<DeckId, \{ fader: number; picks: number \}>\(\);/);
	// A pick that fails after a predecessor landed does not give the fader back over it (Codex P1 BLOCKING, #6691).
	assert.match(body, /const predecessorLanded =\s*\(landedPicks\.get\(deck\) \?\? 0\) - \(holdsLanded \? 1 : 0\) > 0 && landedGeneration\.get\(deck\) === decks\[deck\]\.load_generation;/);
	assert.match(body, /faderUntouched\(\) && !predecessorLanded\) \{\s*const back = await runCommand/);
});

test('CMDK-19: an aligned pick captures its bar before its first wait, and writes no state into a bar reopened meanwhile (Codex r4279465810)', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const body = bar.slice(bar.indexOf('async function loadAligned'), bar.indexOf('function onWindowKeyup'));
	const session = body.indexOf('const session = barSession;');
	assert.ok(session > 0 && session < body.indexOf('await waitDeckIdle('), 'the session is read before the deck wait');
	assert.match(body, /const pairId = \+\+pairLoadSeq;\s*if \(barSession === session\) pairLoads = \[\.\.\.pairLoads, \{ id: pairId, title: row\.title \?\? '\(untitled\)', deck \}\];/);
	assert.match(body, /const fail = \(message: string\): string => \{\s*if \(barSession === session\) actionError = message;/);
});

test('CMDK-19: cmdk_pick reads its own action result from runAction, not the shared bar error (Codex r4279465812)', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const pick = bar.slice(bar.indexOf('pick: async (index, deck, confirm, rowId, targetAt, vocalsOnly, source)'), bar.indexOf('snapshot: () => ({'));
	assert.match(pick, /const outcome = await runAction\(entry\.action, confirm, \(\) => [^;]*\);[\s\S]*if \(outcome !== null\) throw new Error\(outcome\);/);
	assert.doesNotMatch(pick, /throw new Error\(actionError\)/);
	const action = bar.slice(bar.indexOf('async function runAction'), bar.indexOf('/** Enter, a click or a quick pick on any row. */'));
	assert.match(action, /Promise<string \| \{ asked: DeckId \} \| null>/);
	assert.doesNotMatch(action, /actionError = `[^`]*`;\s*return;/);
});

test('CMDK-19: a landed pick keeps the deck landed count until its overlapping prelude group settles (Codex r4280152481)', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const body = bar.slice(bar.indexOf('async function loadAligned'), bar.indexOf('function onWindowKeyup'));
	const fin = body.slice(body.lastIndexOf('} finally {'));
	// No per-pick decrement at the end of the hold: that let a successor's rollback see no partner.
	assert.doesNotMatch(fin, /if \(holdsLanded\) \{\s*const count = /);
	// The count clears with the group: only when the last overlapping pick has settled.
	assert.match(fin, /if \(inPrelude\) \{[\s\S]*else \{\s*preludeBaseline\.delete\(deck\);\s*landedPicks\.delete\(deck\);\s*landedGeneration\.delete\(deck\);\s*\}/);
	// And a landed track a later load replaced no longer counts as a partner (Codex r4280465902).
	assert.match(body, /landedGeneration\.set\(deck, landed\);/);
	assert.match(body, /landedGeneration\.get\(deck\) === decks\[deck\]\.load_generation;/);
});

test('CMDK-15: a setting row in the agent snapshot carries the value it shows and what Enter does to it (Codex P2 BLOCKING, #6691)', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const snap = bar.slice(bar.indexOf('snapshot: () => ({'));
	const rowsPart = snap.slice(snap.indexOf('rows: flat.map('), snap.indexOf('// The aligned pair'));
	assert.match(rowsPart, /entry\.action\.kind === 'setting'/);
	assert.match(rowsPart, /value: settingValueLabel\(entry\.action\.def, entry\.action\.value\)/);
	assert.match(rowsPart, /action: settingVerb\(entry\.action\.def, entry\.action\.value, settingTokens\)/);
	// The same helpers the row draws with, so the snapshot cannot say something the bar does not show.
	assert.match(bar, /\{settingVerb\(action\.def, action\.value, settingTokens\)\}/);
	assert.match(bar, /\{settingValueLabel\(action\.def, action\.value\)\}/);
	for (const f of ['lib/rb/cmdk-actions.ts', 'lib/rb/performance-ipc-types.ts']) {
		const src = readFileSync(`${SRC}${f}`, 'utf8');
		assert.match(src, /value\?: string;/, f);
		assert.match(src, /action\?: string;/, f);
	}
});

test('CMDK-15: the agent snapshot reports the aligned pick in progress or its failure, as the footer shows it (Codex r4280465903)', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	assert.match(bar, /const cmdkOperation = \$derived\.by\(/);
	// The footer checks the failure first, so the snapshot must too (Codex P2 BLOCKING, #6691).
	assert.match(bar, /if \(actionError !== null\) return \{ status: 'error', message: actionError \};\n\t\tif \(pairLoading !== null\) return \{ status: 'loading', title: pairLoading\.title, deck: pairLoading\.deck \};/);
	assert.match(bar, /\.\.\.\(open && cmdkOperation !== null \? \{ operation: cmdkOperation \} : \{\}\),/);
	const types = readFileSync(`${SRC}lib/rb/cmdk-actions.ts`, 'utf8');
	assert.match(types, /operation\?: \{ status: 'loading'; title: string; deck: DeckId \} \| \{ status: 'error'; message: string \};/);
});

test('CMDK-15: query().cmdk reports the open deck picker or eject confirmation, so an agent does not pick rows the DJ cannot see (Codex, #6691)', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const snap = bar.slice(bar.indexOf('snapshot: () => ({'));
	assert.match(snap, /\.\.\.\(open && picker !== null\s*\? \{ dialog: \{ kind: 'deck-picker' as const, deck: picker\.sel, title: picker\.row\.title \?\? '\(untitled\)', confirming: picker\.guard !== null \} \}\s*: open && ejectGuard !== null\s*\? \{ dialog: \{ kind: 'eject' as const, deck: ejectGuard\.id \} \}\s*: \{\}\),/);
	const decl =
		"dialog\\?:\\s*(?:\\|\\s*)?(?:\\n\\s*)?\\{ kind: 'deck-picker'; deck: DeckId; title: string; confirming\\?: boolean \\}\\s*" +
		"\\|\\s*(?:\\n\\s*)?\\{ kind: 'eject'; deck: DeckId \\}";
	assert.match(readFileSync(`${SRC}lib/rb/cmdk-actions.ts`, 'utf8'), new RegExp(decl));
	assert.match(readFileSync(`${SRC}lib/rb/performance-ipc-types.ts`, 'utf8'), new RegExp(decl));
});

test('CMDK-19: an agent open that awaits the first tick does not let an older open write its query over a newer one (Codex P2 BLOCKING, #6691)', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const adapter = bar.slice(bar.indexOf('open: async (text) => {'), bar.indexOf('close: () => closeBar()'));
	assert.match(adapter, /const turn = \+\+openTurn;/);
	assert.ok(adapter.indexOf('if (turn !== openTurn) return;') > adapter.indexOf('await openBar()'), 'the stale check follows the await');
	assert.ok(adapter.indexOf('if (turn !== openTurn) return;') < adapter.indexOf('query = text'), 'the stale check guards the query write');
});

test('CMDK-19: a bar pick read before its deck guard waits does not land in a bar that closed or reopened meanwhile (Codex P2 BLOCKING, #6691)', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const body = bar.slice(bar.indexOf('pick: async (index, deck, confirm, rowId, targetAt, vocalsOnly, source) => {'), bar.indexOf('snapshot: () => ({'));
	const session = body.indexOf('const session = barSession;');
	const guard = body.indexOf('const refused = await pickGuard(');
	assert.ok(session > 0 && session < guard, 'the bar is captured before the guard waits');
	assert.match(body, /if \(barSession !== session \|\| !open\) throw new Error\('the Cmd K bar closed before this pick ran; pick again'\);/);
	assert.ok(body.indexOf('barSession !== session') > guard, 'the fence is checked after the wait');
	assert.ok(body.indexOf('barSession !== session') < body.indexOf('if (refused !== null) throw'), 'the fence runs before the refusal is read');
});

test('CMDK-19: a pick is refused when its bar closed during the deck wait, or its row changed (Codex P2 BLOCKING x2, #6691)', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const body = bar.slice(bar.indexOf('pick: async (index, deck, confirm, rowId, targetAt, vocalsOnly, source) => {'), bar.indexOf('snapshot: () => ({'));
	const guard = body.indexOf('const refused = await pickGuard(');
	const fence = body.indexOf('barSession !== session || !open');
	const reread = body.indexOf('cmdkWireId(flat[index]) !== at');
	assert.ok(fence > guard, 'the close fence is checked after the wait');
	assert.ok(reread > guard, 'the pinned row is read again after the wait');
	assert.ok(fence < body.indexOf('if (refused !== null) throw'), 'both checks run before the refusal is read');
});

test('an eject picked by row re-checks that row after its deck drains (Codex P2 BLOCKING, #6691)', () => {
	const body = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	// The pick hands runAction a check on the row it read; the eject asks it again after the drain, before it unloads.
	assert.match(body, /await runAction\(entry\.action, confirm, \(\) => flat\[index\] !== undefined && cmdkWireId\(flat\[index\]\) === at, source\);/);
	const drain = body.indexOf('if (deckPending(command.deck) > 0) {', body.indexOf('async function runAction'));
	const recheck = body.indexOf('if (!rowStillPicked()) return fail(', drain);
	const unload = body.indexOf("runCommand({ type: 'unload', deck: command.deck }, source)", drain);
	assert.ok(drain > 0 && recheck > drain && unload > recheck, 'the recheck sits between the drain and the unload');
});

test('the picker loads the pair it showed: its basis is passed to the aligned load and checked (Codex P2 BLOCKING, #6691)', () => {
	const bar = readFileSync(new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url), 'utf8');
	assert.match(bar, /if \(pair\?\.aligned === true\) return await loadAligned\(row, deck, confirmed, pair\.basis, vocalsOnly\);/);
	assert.match(bar, /if \(basis !== undefined && pair !== undefined && pair\.basis !== basis\) return fail\(/);
});

test('query().cmdk names the target deck an omitted cmdk_pick deck loads to (Codex P2 BLOCKING, #6691)', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const snap = bar.slice(bar.indexOf('snapshot: () => ({'), bar.indexOf('rows: flat.map('));
	assert.match(snap, /selected,\s*(?:\/\/[^\n]*\s*)*target,/);
	assert.match(readFileSync(`${SRC}lib/rb/cmdk-actions.ts`, 'utf8'), /target: DeckId;/);
});

test('CMDK-19: cmdk_pick with a target refuses when the target deck moved, before and after the guard wait (Codex P2 BLOCKING, #6691)', () => {
	const bar = readFileSync(new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url), 'utf8');
	const body = bar.slice(bar.indexOf('pick: async (index, deck, confirm, rowId, targetAt, vocalsOnly, source)'), bar.indexOf('snapshot: () => ({'));
	assert.match(body, /const targetMoved = \(\) => targetAt !== null && target !== targetAt;/);
	assert.match(body, /if \(targetMoved\(\)\) throw new Error\(`the target deck is now/);
	assert.match(body, /if \(targetMoved\(\)\) throw new Error\(`the target deck moved to/);
	// The later check runs after the guard's wait and before the refusal or the load it guards.
	assert.ok(body.indexOf('the target deck moved to') < body.indexOf('if (refused !== null)'));
});

test('CMDK-19: an eject whose bar closed during the deck drain is refused before it unloads (Codex P2 BLOCKING, #6691)', () => {
	const bar = readFileSync(new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url), 'utf8');
	const body = bar.slice(bar.indexOf('const picked = decks[command.deck].load_generation;'), bar.indexOf('/** Enter, a click or a quick pick on any row. */'));
	const fence = body.indexOf('if (barSession !== session || !open) return fail(');
	assert.ok(fence > 0, 'the eject checks its captured bar session and open state after the drain');
	assert.ok(fence < body.indexOf('if (!rowStillPicked())'), 'the fence runs before the row check');
	assert.ok(fence < body.indexOf("runCommand({ type: 'unload'"), 'the fence runs before the unload');
});

test('CMDK-19: a pair basis changes when the master moves to another deck with the same track (Codex P2 BLOCKING, #6691)', () => {
	const snap = { aTempoRatio: 1, bTempoRatio: 1, aPositionMs: 0, bPositionMs: 0 };
	assert.notEqual(
		pairs.pairBasis('same-track', 1, snap),
		pairs.pairBasis('same-track', 2, snap),
		'the same track on another master deck is another alignment source'
	);
});
