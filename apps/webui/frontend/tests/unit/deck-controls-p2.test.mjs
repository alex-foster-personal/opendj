/**
 * Alpha status P2 (Fri 9 Oct 2026): deck controls that were inert.
 * DECKCTL-6235 auto cue (AU), DECKCTL-6235b manual tempo (MA: type or TAP),
 * DECKCTL-6235c phrase quantize (the Q grid's 'phase'), DECKCTL-6235d loop source.
 *
 * Pure math is tested on the real modules; the wiring checks read the real
 * component and engine sources. Each guard has a control asserting the
 * pre-existing behavior still holds where it should (verification.md:
 * mutate the opposite way too).
 */
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, describe, test } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

const SRC = fileURLToPath(new URL('../../src', import.meta.url));
const read = (rel) => readFileSync(`${SRC}/${rel}`, 'utf8');

/** A 120 BPM 4/4 grid: one beat every 0.5 s from `startSec`. */
function grid(count, startSec = 0.5) {
	return Array.from({ length: count }, (_, i) => ({ t: startSec + i * 0.5, n: (i % 4) + 1, bpm: 120 }));
}

function cue(over) {
	return {
		kind: 'memory',
		slot: null,
		in_ms: 0,
		out_ms: null,
		is_loop: false,
		active_loop: false,
		beat_loop_size: null,
		color_table_index: null,
		comment: null,
		...over
	};
}

let autocue;
let scheduleMath;
let phrase;
let loopSource;
let tempoEdit;
let schedule;

before(async () => {
	autocue = await loadTypeScriptModule('src/lib/player/grid-features.ts');
	scheduleMath = await loadTypeScriptModule('src/lib/player/transport/schedule-math.ts');
	phrase = await loadTypeScriptModule('src/lib/player/transport/loops.ts');
	loopSource = await loadTypeScriptModule('src/lib/rb/loop-source.ts');
	tempoEdit = await loadTypeScriptModule('src/lib/rb/tempo-edit.ts');
	schedule = await loadTypeScriptModule('src/lib/player/transport/schedule-math.ts');
});

describe('DECKCTL-6235 auto cue', () => {
	test('parks at the earliest memory cue, not a hot cue', () => {
		const anlz = {
			cues: [cue({ kind: 'hot_cue', slot: 'A', in_ms: 1000 }), cue({ in_ms: 9000 }), cue({ in_ms: 4000 })],
			beatgrid: { beats: grid(8) }
		};
		assert.equal(autocue.autoCueTargetMs(anlz, 60000), 4000);
	});

	// Codex P2 on PR #6235: rekordbox Kind 0 rows with an out point arrive as
	// kind "loop" (slot null); a memory loop is a memory entry for Auto Cue.
	test('an earlier memory loop counts as a memory entry; a hot-cue loop still does not', () => {
		const anlz = {
			cues: [
				cue({ kind: 'hot_cue', slot: 'A', in_ms: 500, out_ms: 2500, is_loop: true }),
				cue({ in_ms: 9000 }),
				cue({ kind: 'loop', in_ms: 3000, out_ms: 5000, is_loop: true })
			],
			beatgrid: { beats: grid(8) }
		};
		assert.equal(autocue.autoCueTargetMs(anlz, 60000), 3000);
	});

	test('with no memory cue it parks on the first beat, and never past the end', () => {
		assert.equal(autocue.autoCueTargetMs({ cues: [], beatgrid: { beats: grid(8, 1.25) } }, 60000), 1250);
		assert.equal(autocue.autoCueTargetMs({ cues: [cue({ in_ms: 70000 })], beatgrid: { beats: grid(8, 1.25) } }, 60000), 1250);
	});

	test('control: a beat at 0:00, no grid or no analysis leaves the load at 0:00', () => {
		assert.equal(autocue.autoCueTargetMs({ cues: [], beatgrid: { beats: grid(8, 0) } }, 60000), null);
		assert.equal(autocue.autoCueTargetMs({ cues: [], beatgrid: { beats: [] } }, 60000), null);
		assert.equal(autocue.autoCueTargetMs(null, 60000), null);
	});

	test('a finished track restarts at its cue only while AU is on', () => {
		assert.equal(scheduleMath.autoCueRestartSec(true, 4000, 300), 4);
		assert.equal(scheduleMath.autoCueRestartSec(false, 4000, 300), 0, 'AU off keeps the old restart at 0:00');
		assert.equal(scheduleMath.autoCueRestartSec(true, null, 300), 0);
		assert.equal(scheduleMath.autoCueRestartSec(true, 400000, 300), 0, 'a cue past the end cannot be a restart point');
	});

	test('playResumePositionSec restarts a finished track at the given point and leaves a mid-track resume alone', () => {
		assert.equal(schedule.playResumePositionSec(300, 300, null, 4), 4);
		assert.equal(schedule.playResumePositionSec(300, 300, null), 0, 'default stays 0:00');
		assert.equal(schedule.playResumePositionSec(120, 300, null, 4), 120);
	});

	test('wiring: the load path parks through the engine, play passes the restart point, and AU toggles the pref', () => {
		const ipc = read('lib/rb/performance-ipc.svelte.ts');
		assert.match(ipc, /await _quantizeOnForNewTrack\(command\.deck\);\s*_autoCueNewTrack\(command\.deck\);/);
		assert.match(ipc, /if \(!uiPrefs\.deck_autocue\) return;[\s\S]{0,200}engine\.parkAtCue\(deck, target\)/);
		const engine = read('lib/rb/audio-engine.svelte.ts');
		assert.match(engine, /autoCueRestartSec\(uiPrefs\.deck_autocue, st\.cue_ms, rt\.durationSec\)/);
		assert.equal((engine.match(/st\.loop,\s*restartSec\s*\)/g) ?? []).length, 2, 'both play() resume paths restart at the cue');
		const jog = read('lib/components/rb/deck/JogDial.svelte');
		// Codex P1 on PR #6235: AU goes through the dispatcher, so a preset's
		// lifecycle lock rejects it like any other control.
		assert.match(jog, /onclick=\{\(\) => onAutoCue\(!uiPrefs\.deck_autocue\)\}/);
		const deck = read('lib/components/rb/Deck.svelte');
		assert.match(deck, /onAutoCue=\{\(enabled\) => void runPerformanceCommandFromUi\(\{ type: 'deck_autocue', enabled \}\)\}/);
		assert.doesNotMatch(jog, /setAutoCue/);
		assert.match(ipc, /command\.type === 'deck_autocue'\) \{\n\t\tawait setAutoCue\(command\.enabled\);/);
		assert.doesNotMatch(jog, /plannedTitle\('auto-cue'\)/);
		// Codex P2 on PR #6235: the Rust engine's load returns before
		// _autoCueNewTrack, so AU is inert there with a PARITY-TODO title.
		assert.match(ipc, /if \(await executeInRustEngine\(command, pushToast\)\) \{\n\t\tif \(command\.type === 'load'\) await _quantizeOnForNewTrack\(command\.deck\);\n\t\treturn;/);
		assert.match(jog, /class:rb-inert=\{rustMode\.enabled\}\n\t\t\tdisabled=\{rustMode\.enabled\}\n\t\t\taria-pressed=\{uiPrefs\.deck_autocue\}/);
		assert.match(jog, /rustMode\.enabled\n\t\t\t\? 'Auto cue - not implemented in the Rust engine mode yet - see PARITY-TODO'/);
		// Control: Web Audio mode still lights AU from the pref.
		assert.match(jog, /class:lit=\{uiPrefs\.deck_autocue && !rustMode\.enabled\}/);
	});

	test('deck_autocue parses like any IPC input; grid_edit queues on the deck persistence scope', async () => {
		const ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts', {
			viteApiBase: 'https://deck-controls-p2.example.test'
		});
		assert.deepEqual(ipc.parsePerformanceCommandForTest({ type: 'deck_autocue', enabled: true }), { type: 'deck_autocue', enabled: true });
		assert.throws(() => ipc.parsePerformanceCommandForTest({ type: 'deck_autocue', enabled: 'yes' }));
		assert.throws(() => ipc.parsePerformanceCommandForTest({ type: 'deck_autocue', enabled: true, deck: 1 }));
		assert.equal(ipc.performanceCommandQueueScopes({ type: 'deck_autocue', enabled: true }), null);
		// Codex P1 on PR #6235: a preset's all-scope barrier must wait for an
		// edit in flight, which the persistence scope gives without making
		// transport on that deck wait behind the round trip.
		const edit = { type: 'grid_edit', deck: 2, edit: { kind: 'nudge', offset_ms: 2, beats: 0 } };
		assert.deepEqual(ipc.performanceCommandQueueScopes(edit), ['persistence-2']);
		assert.ok(ipc.PERFORMANCE_PRESET_COMMAND_SCOPES.includes('persistence-2'));
		// Control: transport on the same deck keeps its own scope.
		assert.ok(!ipc.performanceCommandQueueScopes({ type: 'cue', deck: 2 }).includes('persistence-2'));
	});
});

describe('DECKCTL-6235b manual tempo', () => {
	test('four taps half a second apart read 120 BPM', () => {
		let taps = [];
		for (const t of [1000, 1500, 2000]) taps = tempoEdit.pushTempoTap(taps, t);
		assert.equal(tempoEdit.tapTempoBpm(taps), null, 'three taps are not enough');
		taps = tempoEdit.pushTempoTap(taps, 2500);
		assert.equal(tempoEdit.tapTempoBpm(taps), 120);
	});

	test('a pause longer than 2 s, or a clock that went back, starts a new run', () => {
		let taps = [1000, 1500, 2000, 2500];
		taps = tempoEdit.pushTempoTap(taps, 5000);
		assert.deepEqual(taps, [5000]);
		assert.deepEqual(tempoEdit.pushTempoTap([1000, 1500], 1200), [1200]);
	});

	test('the run keeps only the latest window of intervals', () => {
		let taps = [];
		for (let i = 0; i < 20; i++) taps = tempoEdit.pushTempoTap(taps, 1000 + i * 400);
		assert.equal(taps.length, tempoEdit.TAP_TEMPO_WINDOW + 1);
		assert.equal(tempoEdit.tapTempoBpm(taps), 150);
	});

	test('wiring: MA opens the tempo editor, which has a TAP button using the same writes as a typed BPM', () => {
		const jog = read('lib/components/rb/deck/JogDial.svelte');
		assert.match(jog, /<TempoEditModal\s+deckId=\{deck\.deck_id\}/);
		assert.doesNotMatch(jog, /plannedTitle\('manual-source'\)/);
		const modal = read('lib/components/rb/deck/TempoEditModal.svelte');
		assert.match(modal, /data-testid=\{`tempo-edit-tap-deck-\$\{id\}`\}/);
		assert.match(modal, /tapApplied = await _dispatchWrites\(id, bpm, event\.timeStamp\)/);
		assert.match(modal, /the tempo did not change/, 'a tap that cannot apply says so instead of claiming it did');
	});
});

describe('DECKCTL-6235c phrase quantize', () => {
	const beats = grid(64, 0.5); // 0.5 s .. 32 s
	const phrases = [
		{ start_s: 0.52, end_s: 8.49, kind: 1, mood: 1 },
		{ start_s: 8.49, end_s: 16.5, kind: 2, mood: 1 },
		{ start_s: 16.5, end_s: 24.5, kind: 3, mood: 1 }
	];

	test('boundaries are phrase starts plus the last end, each on a real beat', () => {
		assert.deepEqual(phrase.phraseBoundaries(beats, phrases).onGrid, [500, 8500, 16500, 24500]);
	});

	test("'phase' snaps to the nearest phrase boundary, not the nearest beat", () => {
		assert.equal(phrase.snappedPositionMs(beats, phrases, 11000, 'phase'), 8500);
		assert.equal(phrase.snappedPositionMs(beats, phrases, 13000, 'phase'), 16500);
	});

	test('control: 1/4 still snap to the beat grid exactly as before', () => {
		assert.equal(phrase.snappedPositionMs(beats, phrases, 11100, 1), 11000);
		assert.equal(phrase.snappedPositionMs(beats, phrases, 11100, 4), 10500);
	});

	// Codex P1 on PR #6235: phrase analysis can run past a short or truncated
	// grid. Those boundaries have no beat, so they must not clamp onto the last
	// beat and drag a late seek back by minutes.
	describe('phrases beyond the grid span', () => {
		const long = [
			...phrases,
			{ start_s: 24.5, end_s: 33.0, kind: 1, mood: 1 },
			{ start_s: 33.0, end_s: 180, kind: 2, mood: 1 },
			{ start_s: 180, end_s: 240, kind: 3, mood: 1 }
		];
		test('off-grid boundaries are left out, not clamped to the last beat', () => {
			assert.deepEqual(phrase.phraseBoundaries(beats, long), {
				onGrid: [500, 8500, 16500, 24500],
				offGridMs: [33000, 180000, 240000]
			});
		});
		test('a position past the grid stays put', () => {
			assert.equal(phrase.snappedPositionMs(beats, long, 200000, 'phase'), 200000);
			assert.equal(phrase.phraseSeekDecisionMs(beats, long, 200000, false, null).targetMs, 200000);
			// Even when every phrase ends inside the grid (audio runs past both).
			assert.equal(phrase.snappedPositionMs(beats, phrases, 200000, 'phase'), 200000);
		});
		test('an in-span position whose nearest boundary is off the grid stays put', () => {
			assert.equal(phrase.snappedPositionMs(beats, long, 32200, 'phase'), 32200);
		});
		test('control: an in-span position still snaps to its on-grid boundary', () => {
			assert.equal(phrase.snappedPositionMs(beats, long, 26000, 'phase'), 24500);
			assert.equal(phrase.snappedPositionMs(beats, long, 11000, 'phase'), 8500);
		});
	});

	test('no phrases falls back to the bar, never to no snap', () => {
		assert.equal(phrase.snappedPositionMs(beats, [], 11100, 'phase'), 10500);
		assert.match(phrase.phraseQuantizeTip([]), /snaps to the bar/);
	});

	test('a seek leaving a loop still exits it, and skip passes the target through', () => {
		const loop = { in_ms: 1000, out_ms: 3000, engaged: true };
		assert.deepEqual(phrase.phraseSeekDecisionMs(beats, phrases, 11000, false, loop), { targetMs: 8500, exitLoop: true });
		assert.deepEqual(phrase.phraseSeekDecisionMs(beats, phrases, 2000, true, loop), { targetMs: 2000, exitLoop: false });
	});

	test('loops keep whole beats under phase', () => {
		assert.equal(phrase.loopGridBeats('phase'), 1);
		assert.equal(phrase.loopGridBeats(8), 8);
	});
});

describe('DECKCTL-6235d loop source', () => {
	const cues = [
		cue({ in_ms: 30000, out_ms: 34000, is_loop: true }),
		cue({ kind: 'hot_cue', slot: 'B', in_ms: 5000, out_ms: 7000, is_loop: true }),
		cue({ in_ms: 10000, out_ms: 12000, is_loop: true, active_loop: true, comment: 'drop' }),
		cue({ kind: 'hot_cue', slot: 'A', in_ms: 1000 })
	];

	test('lists hot-cue loops by letter, then memory loops with the active one first; plain cues are not loops', () => {
		const loops = loopSource.storedLoops(cues);
		assert.deepEqual(loops.map((l) => l.short), ['HOT B', 'MEM', 'MEM']);
		assert.equal(loops[1].in_ms, 10000);
		assert.match(loops[1].label, /^Active memory loop .* - drop$/);
	});

	test('the engaged loop names its stored source, and a grid loop reads INT', () => {
		const loops = loopSource.storedLoops(cues);
		assert.equal(loopSource.engagedStoredLoop(loops, { in_ms: 5000.02, out_ms: 6999.98, engaged: true })?.short, 'HOT B', 'frame rounding still matches');
		// Codex P2 on PR #6235: stored loops engage exactly, so a grid loop 20 ms
		// off a stored one is an INT loop, not that stored loop.
		assert.equal(loopSource.engagedStoredLoop(loops, { in_ms: 5020, out_ms: 6980, engaged: true }), null);
		assert.equal(loopSource.engagedStoredLoop(loops, { in_ms: 5000, out_ms: 9000, engaged: true }), null);
		assert.equal(loopSource.engagedStoredLoop(loops, { in_ms: 5000, out_ms: 7000, engaged: false }), null);
	});

	// Codex P2 on PR #6235: hot cue A and a memory loop can share one range;
	// the row the DJ picked is the one the button names.
	test('a range two stored loops share names the picked one; with no pick, the first', () => {
		const shared = loopSource.storedLoops([
			cue({ kind: 'hot_cue', slot: 'A', in_ms: 5000, out_ms: 7000, is_loop: true }),
			cue({ in_ms: 5000, out_ms: 7000, is_loop: true })
		]);
		const engaged = { in_ms: 5000, out_ms: 7000, engaged: true };
		const memory = shared.find((loop) => loop.source === 'memory');
		assert.equal(loopSource.engagedStoredLoop(shared, engaged, memory.id)?.short, 'MEM');
		assert.equal(loopSource.engagedStoredLoop(shared, engaged, shared[0].id)?.short, 'HOT A');
		assert.equal(loopSource.engagedStoredLoop(shared, engaged)?.short, 'HOT A', 'an agent-engaged range reads the first match');
		assert.equal(loopSource.engagedStoredLoop(shared, { ...engaged, out_ms: 8000 }, memory.id), null, 'a pick never names a range it does not match');
		const menu = read('lib/components/rb/deck/LoopSourceMenu.svelte');
		assert.match(menu, /engagedStoredLoop\(loops, deck\.loop, pickedId\)/);
		assert.match(menu, /const id = await pickedAfter\(\(\) => onStoredLoop\(\{ in_ms: loop\.in_ms, out_ms: loop\.out_ms \}\), pickedId, loop\.id\);\n\t\tpicked = \{ load, id, seen: false, since \};/);
	});

	// Codex P2 on PR #6235: the readout or interval grid can engage an INT loop
	// whose beat-aligned range equals the stored loop picked earlier; the button
	// must read INT, whatever row was picked.
	test('an INT loop on a stored range reads INT, even with that row picked', () => {
		const loops = loopSource.storedLoops([cue({ in_ms: 5000, out_ms: 7000, is_loop: true })]);
		const picked = loops[0].id;
		const stored = { in_ms: 5000, out_ms: 7000, engaged: true, beat_length: null };
		assert.equal(loopSource.engagedStoredLoop(loops, stored, picked)?.short, 'MEM', 'control: the stored engagement still names it');
		assert.equal(loopSource.engagedStoredLoop(loops, { ...stored, beat_length: 4 }, picked), null);
		assert.equal(loopSource.engagedStoredLoop(loops, { ...stored, beat_length: 4 }), null);
		// The provenance this rests on: a stored range engages through setLoop,
		// which leaves beat_length null; INT engages with its beat count.
		const engine = read('lib/rb/audio-engine.svelte.ts');
		assert.match(
			engine,
			/async setLoop\(deck: DeckId, loop: \{ in_ms: number; out_ms: number \} \| null, quantize = true\): Promise<void> \{\n\t\t(?:_abandonRoll\(deck\);\n\t\t)?await this\._setLoop\(deck, loop, null, quantize\);/
		);
		assert.match(
			engine,
			/(?:_abandonRoll\(deck\);\n\t\t)?await this\._setLoop\(deck, range, beats\);/
		);
	});

	// Codex P2 on PR #6235: a refused engage (a preset owns the controls) must
	// not relabel the stored loop still in force; nor may a refused INT clear it.
	// Codex P2 on PR #6235: two memory-loop rows with one range got one id,
	// which the keyed menu and the picked row cannot tell apart.
	test('stored loops sharing a range still get distinct, stable ids', () => {
		const rows = [
			cue({ in_ms: 5000, out_ms: 7000, is_loop: true }),
			cue({ in_ms: 5000, out_ms: 7000, is_loop: true, comment: 'drop' }),
			cue({ in_ms: 9000, out_ms: 11000, is_loop: true })
		];
		const ids = loopSource.storedLoops(rows).map((loop) => loop.id);
		assert.equal(new Set(ids).size, 3, `ids: ${ids}`);
		assert.deepEqual(loopSource.storedLoops(rows).map((loop) => loop.id), ids, 'the same cues give the same ids');
		// Control: a loop with no twin keeps its plain id.
		assert.ok(ids.includes('mem-9000-11000'));
		assert.ok(ids.includes('mem-5000-7000'));
	});

	test('the picked row changes only when the engine took the command', async () => {
		assert.equal(await loopSource.pickedAfter(async () => false, 'hot-A', 'mem-1'), 'hot-A');
		assert.equal(await loopSource.pickedAfter(async () => true, 'hot-A', 'mem-1'), 'mem-1', 'control: an accepted pick moves');
		assert.equal(await loopSource.pickedAfter(async () => false, 'mem-1', null), 'mem-1');
		assert.equal(await loopSource.pickedAfter(async () => true, 'mem-1', null), null, 'control: an accepted INT clears');
		const menu = read('lib/components/rb/deck/LoopSourceMenu.svelte');
		assert.match(menu, /const id = inForce === null \? null : await pickedAfter\(onDisengage, pickedId, null\);/);
		assert.doesNotMatch(menu, /pickedId = loop\.id;/);
		const deck = read('lib/components/rb/Deck.svelte');
		assert.match(deck, /return \(await runPerformanceCommandFromUi\(\{ type: 'loop', deck: deckId, loop, quantize: false \}\)\)\.ok;/);
		assert.match(deck, /return \(await runPerformanceCommandFromUi\(\{ type: 'loop', deck: deckId, loop: null \}\)\)\.ok;/);
	});

	// Codex P2 on PR #6235: ids repeat across tracks, so a pick on track A
	// must not name a row on track B that happens to share its id.
	test('a pick names a row only on the load it was made on', () => {
		const picked = { load: 3, id: 'mem-5000-7000' };
		assert.equal(loopSource.pickedForLoad(picked, 3), 'mem-5000-7000', 'control: the same load keeps it');
		assert.equal(loopSource.pickedForLoad(picked, 4), null);
		const menu = read('lib/components/rb/deck/LoopSourceMenu.svelte');
		assert.match(menu, /const pickedId: string \| null = \$derived\(pickedForLoad\(picked, deck\.load_generation\)\);/);
		// The load is read before the await, so a load during it drops the pick.
		assert.match(menu, /const load = deck\.load_generation;\n\t\tconst since = loopStateKey\(deck\.loop\);\n\t\tconst id = await pickedAfter/);
	});

	// Codex P2 on PR #6235 (three times): a pick that left force (exited from
	// MIDI, the readout or an agent), or was superseded before it ever showed,
	// even by a queued loop command that ran before the pick's acknowledgement
	// was handled, must not label a later loop on the same range.
	test('a pick ends once the loop it named stops being in force', () => {
		const key = loopSource.loopStateKey;
		const beforeSend = key({ in_ms: 1000, out_ms: 2000, engaged: true, beat_length: 4 });
		const fresh = { load: 3, id: 'mem-5000-7000~2', seen: false, since: beforeSend };
		const shown = key({ in_ms: 5000, out_ms: 7000, engaged: true, beat_length: null });
		const seen = loopSource.pickedWhileInForce(fresh, 'mem-5000-7000~2', shown);
		assert.deepEqual(seen, { ...fresh, seen: true });
		assert.equal(loopSource.pickedWhileInForce(seen, 'mem-5000-7000~2', shown), seen, 'control: still in force, unchanged');
		assert.deepEqual(loopSource.pickedWhileInForce(seen, null, key(null)), { ...fresh, id: null, seen: false }, 'exited: dropped');
		assert.deepEqual(loopSource.pickedWhileInForce(seen, 'hot-A', shown), { ...fresh, id: null, seen: false }, 'moved: dropped');
		assert.equal(loopSource.pickedWhileInForce(fresh, null, beforeSend), fresh, 'control: engage not applied yet, kept');
		const successor = key({ in_ms: 9000, out_ms: 11000, engaged: true, beat_length: null });
		assert.deepEqual(
			loopSource.pickedWhileInForce(fresh, null, successor),
			{ ...fresh, id: null, seen: false },
			'superseded before it showed (even before the ack was handled): dropped'
		);
		// The key is by value, so an in-place edit of the loop still counts.
		assert.notEqual(key({ in_ms: 1000, out_ms: 2000, engaged: true, beat_length: 4 }), key({ in_ms: 1000, out_ms: 2000, engaged: false, beat_length: 4 }));
		assert.notEqual(key({ in_ms: 1000, out_ms: 2000, engaged: true, beat_length: 4 }), key({ in_ms: 1000, out_ms: 2000, engaged: true, beat_length: null }));
		const menu = read('lib/components/rb/deck/LoopSourceMenu.svelte');
		assert.match(menu, /const next = pickedWhileInForce\(picked, inForce\?\.id \?\? null, loopStateKey\(deck\.loop\)\);\n\t\tif \(next !== picked\) picked = next;/);
		// `since` is sampled BEFORE the command is sent, in both picks.
		assert.equal((menu.match(/const since = loopStateKey\(deck\.loop\);\n\t\tconst id = /g) ?? []).length, 2);
		assert.equal((menu.match(/picked = \{ load, id, seen: false, since \};/g) ?? []).length, 2);
	});

	test('wiring: the menu engages through the loop command, and INT is no longer inert', () => {
		const cluster = read('lib/components/rb/deck/LoopCluster.svelte');
		assert.match(cluster, /<LoopSourceMenu \{deck\} \{deckId\} \{pending\} \{onStoredLoop\} \{onDisengage\} \{onSetActive\} \/>/);
		assert.doesNotMatch(cluster, /plannedTitle\('loop-source'\)/);
		const menu = read('lib/components/rb/deck/LoopSourceMenu.svelte');
		assert.match(menu, /loop-source-active-/);
		assert.match(menu, /'Clear active' : 'Set active'/);
		const deck = read('lib/components/rb/Deck.svelte');
		assert.match(deck, /runPerformanceCommandFromUi\(\{ type: 'loop', deck: deckId, loop, quantize: false \}\)/);
		assert.match(deck, /onSetActive=\{setActiveMemoryLoop\}/);
		const engine = read('lib/rb/audio-engine.svelte.ts');
		assert.match(engine, /displayLoopFrom\(publishedAnlz\.cues[^\n]*\n[^\n]*\n\s*_armActiveLoop\(st,/);
		assert.match(engine, /const nextLoop = safetyLoopToEngage\(/);
	});

	// Codex P1 on PR #6235: with Quantize on (the default), a stored loop sent
	// as an ordinary manual loop snapped both ends to the loop grid, so an
	// off-grid saved loop engaged a different range or collapsed.
	test('a stored loop carries quantize false through the dispatcher, and only false is accepted', async () => {
		const ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts', {
			viteApiBase: 'https://deck-controls-p2.example.test'
		});
		const loop = { in_ms: 1100, out_ms: 2900 };
		assert.deepEqual(ipc.parsePerformanceCommandForTest({ type: 'loop', deck: 1, loop, quantize: false }), {
			type: 'loop', deck: 1, loop, quantize: false
		});
		// Control: a manual loop keeps the ordinary (snapped) shape.
		assert.deepEqual(ipc.parsePerformanceCommandForTest({ type: 'loop', deck: 1, loop }), { type: 'loop', deck: 1, loop });
		assert.throws(() => ipc.parsePerformanceCommandForTest({ type: 'loop', deck: 1, loop, quantize: true }), /quantize may only be false/);
		// A loop exit validates quantize the same way (Sol P1, #6235); quantize false still exits.
		assert.throws(() => ipc.parsePerformanceCommandForTest({ type: 'loop', deck: 1, loop: null, quantize: true }), /quantize may only be false/);
		assert.deepEqual(ipc.parsePerformanceCommandForTest({ type: 'loop', deck: 1, loop: null, quantize: false }), { type: 'loop', deck: 1, loop: null });
		assert.deepEqual(ipc.parsePerformanceCommandForTest({ type: 'loop', deck: 1, loop: null }), { type: 'loop', deck: 1, loop: null });
	});

	test('the engine skips the loop-grid snap only for an unquantized loop', () => {
		const engine = read('lib/rb/audio-engine.svelte.ts');
		assert.match(engine, /loopBeats !== null && beatLength === null && quantize \? loopGridBeats\(st\.quantize_grid_beats\) : null/);
		assert.match(engine, /await this\._setLoop\(deck, loop, null, quantize\);/);
		const ipcSrc = read('lib/rb/performance-ipc.svelte.ts');
		assert.match(ipcSrc, /await engine\.setLoop\(command\.deck, command\.loop, command\.quantize !== false\);/);
	});
});

// Codex P1 on PR #6235: the DECKCTL-6235 agent case (PUT deck_autocue) must reach
// an open page, which only hydrated the preference once at boot.
describe('DECKCTL-6235 agent preference writes reach an open page', () => {
	test('a ui_prefs change re-reads AU from disk, and events mid-read owe exactly one more read', async () => {
		const hydrate = await loadTypeScriptModule('src/lib/rb/prefs-hydrate.ts', {
			viteApiBase: 'https://deck-controls-p2-au.example.test'
		});
		const original = globalThis.fetch;
		let reads = 0;
		let disk = true;
		globalThis.fetch = async () => {
			reads += 1;
			return new Response(JSON.stringify({ deck_autocue: disk }), { status: 200, headers: { 'content-type': 'application/json' } });
		};
		try {
			const prefs = { deck_autocue: false };
			let persisted = 0;
			const refresh = hydrate.makeAutoCueRefresher(prefs, () => { persisted += 1; }, (message) => assert.fail(message), 0);
			await Promise.all([refresh(), refresh(), refresh()]);
			assert.equal(prefs.deck_autocue, true, 'the agent write lights AU');
			assert.equal(reads, 2, 'events during one read coalesce into one owed read');
			assert.equal(persisted, 1);
			// Control: a lone event reads once and stops.
			reads = 0;
			await refresh();
			assert.equal(reads, 1);
			assert.equal(persisted, 1, 'an unchanged disk value writes nothing back');
			disk = false;
			await refresh();
			assert.equal(prefs.deck_autocue, false, 'an agent can turn AU off too');
		} finally {
			globalThis.fetch = original;
		}
	});

	test('a second write landing after the in-flight read captured the first still wins', async () => {
		const hydrate = await loadTypeScriptModule('src/lib/rb/prefs-hydrate.ts', {
			viteApiBase: 'https://deck-controls-p2-au-race.example.test'
		});
		const original = globalThis.fetch;
		let disk = true;
		let release;
		const held = new Promise((resolve) => { release = resolve; });
		let reads = 0;
		globalThis.fetch = async () => {
			reads += 1;
			const value = disk;
			if (reads === 1) await held;
			return new Response(JSON.stringify({ deck_autocue: value }), { status: 200, headers: { 'content-type': 'application/json' } });
		};
		try {
			const prefs = { deck_autocue: false };
			const refresh = hydrate.makeAutoCueRefresher(prefs, () => {}, (message) => assert.fail(message), 0);
			const first = refresh();
			await new Promise((resolve) => setTimeout(resolve, 0));
			disk = false;
			const second = refresh();
			release();
			await Promise.all([first, second]);
			assert.equal(reads, 2);
			assert.equal(prefs.deck_autocue, false, 'the later agent write is what AU shows');
		} finally {
			globalThis.fetch = original;
		}
	});

	test('a re-read answered without a boolean deck_autocue fails instead of confirming', async () => {
		// Sol P1 on PR #6235: a 200 missing the field must not read as confirmed.
		const hydrate = await loadTypeScriptModule('src/lib/rb/prefs-hydrate.ts', {
			viteApiBase: 'https://deck-controls-p2-au-shape.example.test'
		});
		const original = globalThis.fetch;
		let answer = {};
		globalThis.fetch = async () =>
			new Response(JSON.stringify(answer), { status: 200, headers: { 'content-type': 'application/json' } });
		try {
			const reports = [];
			const prefs = { deck_autocue: true };
			const refresh = hydrate.makeAutoCueRefresher(prefs, () => {}, (message) => reports.push(message), 0);
			assert.equal(await refresh(), false, 'a missing field is a failed read');
			answer = { deck_autocue: 1 };
			assert.equal(await refresh(), false, 'a non-boolean is a failed read');
			assert.equal(reports.length, 2);
			assert.equal(prefs.deck_autocue, true, 'and changes nothing');
			// Control: a boolean answer confirms and applies.
			answer = { deck_autocue: false };
			assert.equal(await refresh(), true);
			assert.equal(prefs.deck_autocue, false);
		} finally {
			globalThis.fetch = original;
		}
	});

	test('a failed re-read retries, and says so out loud only when every attempt fails', async () => {
		const hydrate = await loadTypeScriptModule('src/lib/rb/prefs-hydrate.ts', {
			viteApiBase: 'https://deck-controls-p2-au-fail.example.test'
		});
		const original = globalThis.fetch;
		let reads = 0;
		let failuresLeft = 0;
		globalThis.fetch = async () => {
			reads += 1;
			if (failuresLeft > 0) {
				failuresLeft -= 1;
				return new Response('down', { status: 503 });
			}
			return new Response(JSON.stringify({ deck_autocue: true }), { status: 200, headers: { 'content-type': 'application/json' } });
		};
		try {
			const reports = [];
			const prefs = { deck_autocue: false };
			const refresh = hydrate.makeAutoCueRefresher(prefs, () => {}, (message, cause) => reports.push({ message, cause }), 0);
			// Control: failures short of the budget recover silently.
			failuresLeft = hydrate.AUTO_CUE_READ_ATTEMPTS - 1;
			assert.equal(await refresh(), true, 'a read that landed resolves true');
			assert.equal(reads, hydrate.AUTO_CUE_READ_ATTEMPTS);
			assert.equal(prefs.deck_autocue, true);
			assert.deepEqual(reports, []);
			// Every attempt failing reports once, naming that AU may be stale.
			reads = 0;
			prefs.deck_autocue = false;
			failuresLeft = hydrate.AUTO_CUE_READ_ATTEMPTS;
			// Codex P2 on PR #6235: the AU command must be able to tell.
			assert.equal(await refresh(), false, 'a read that never landed resolves false');
			assert.equal(reads, hydrate.AUTO_CUE_READ_ATTEMPTS, 'retries are bounded');
			assert.equal(reports.length, 1);
			assert.match(reports[0].message, /AU may be stale/);
			assert.equal(prefs.deck_autocue, false);
		} finally {
			globalThis.fetch = original;
		}
	});

	test('wiring: each jog dial re-reads AU on ui_prefs changes and on every event-bus resync', () => {
		const jog = read('lib/components/rb/deck/JogDial.svelte');
		assert.match(jog, /const offKind = subscribeKind\('ui_prefs', \(\) => void refreshAutoCueFromDisk\(\)\);/);
		// Codex P2 on PR #6235: a resync replays nothing, so it must re-read too.
		assert.match(jog, /const offResync = subscribeResync\(\(\) => void refreshAutoCueFromDisk\(\)\);/);
		const prefs = read('lib/rb/prefs.svelte.ts');
		assert.match(prefs, /export const refreshAutoCueFromDisk = makeAutoCueRefresher\(\s*uiPrefs,\s*_persist,\s*reportSettingSaveError\s*\);/);
	});
});

// Codex P2 on PR #6235: events-bus.ts replays nothing after a connect,
// reconnect or seq gap, so every deck control that refreshes on a library
// kind must refresh on resync too. Read live, so a new deck consumer is held
// to it; the control proves the scan sees the consumers it guards.
test('every deck component that subscribes to a library kind also handles resync', () => {
	const dir = `${SRC}/lib/components/rb/deck`;
	const consumers = readdirSync(dir)
		.filter((name) => name.endsWith('.svelte'))
		.map((name) => [name, readFileSync(`${dir}/${name}`, 'utf8')])
		.filter(([, text]) => /subscribeKind\(/.test(text));
	const names = consumers.map(([name]) => name);
	assert.ok(names.includes('JogDial.svelte') && names.includes('GridEditButtons.svelte'), `consumers: ${names}`);
	const missing = consumers.filter(([, text]) => !/subscribeResync\(/.test(text)).map(([name]) => name);
	assert.deepEqual(missing, []);
	const grid = read('lib/components/rb/deck/GridEditButtons.svelte');
	assert.match(grid, /const offResync = subscribeResync\(\(\) => \{\n\t\t\tconst id = deck\.stable_id;\n\t\t\tif \(id !== null\) onGridEditEvent\(id, reportError\);/);
});

// Codex P1 on PR #6235: an agent that switches AU must be able to read it back
// through the same query surface, as it can show_stems and AutoPlay.
test('a deck_autocue command is read back from query().ui and the mirror publishes it', async () => {
	const store = new Map();
	const storage = {
		getItem: (key) => (store.has(key) ? store.get(key) : null),
		setItem: (key, value) => store.set(key, value),
		removeItem: (key) => store.delete(key)
	};
	const originalStorage = globalThis.localStorage;
	const originalFetch = globalThis.fetch;
	globalThis.localStorage = storage;
	globalThis.window = { localStorage: storage };
	// A ui-prefs stand-in: a PUT lands on disk and a GET answers it, as the
	// command now re-reads AU before confirming (Codex P2s on PR #6235).
	const disk = { deck_autocue: false };
	globalThis.fetch = async (input, init) => {
		const method = (input instanceof Request ? input.method : init?.method) ?? 'GET';
		const raw = input instanceof Request ? await input.clone().text() : init?.body;
		const body = raw ? JSON.parse(raw) : {};
		if (method === 'PUT' && typeof body.deck_autocue === 'boolean') disk.deck_autocue = body.deck_autocue;
		const answer = method === 'GET' ? { ...body, ...disk } : body;
		return new Response(JSON.stringify(answer), { status: 200, headers: { 'content-type': 'application/json' } });
	};
	const ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts', {
		viteApiBase: 'https://deck-controls-p2-readback.example.test'
	});
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		const before = ipc.queryPerformanceState().ui.deck_autocue;
		assert.equal(typeof before, 'boolean');
		await window.musicDjToolsPerformance.dispatch({ type: 'deck_autocue', enabled: !before });
		assert.equal(ipc.queryPerformanceState().ui.deck_autocue, !before);
		// Control: the opposite direction reads back too, so this is not a latch.
		await window.musicDjToolsPerformance.dispatch({ type: 'deck_autocue', enabled: before });
		assert.equal(ipc.queryPerformanceState().ui.deck_autocue, before);
	} finally {
		uninstall();
		delete globalThis.window;
		globalThis.fetch = originalFetch;
		if (originalStorage === undefined) delete globalThis.localStorage;
		else globalThis.localStorage = originalStorage;
	}
	assert.match(read('lib/rb/ui-mirror.ts'), /deck_autocue: state\.ui\.deck_autocue,/);
});
