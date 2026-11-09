// requirement: FXMOD-10603
// [if] attachModFx does not post the lfoAnchor anchor at once on engage [then] broken
// [if] a tempo move under 0.05 BPM posts, or one of 0.05 BPM or more does not, or the re-anchor steps the phase [then] broken
// [if] a playhead jump over 50 ms does not post a fresh anchor with one wet crossfade, or a 40 ms drift does [then] broken
// [if] no grid, no downbeat or no tempo in 40..300 BPM without time_ms is not inert with a named reason [then] broken
// [if] the tempo vanishing mid-effect drops or changes the last anchor [then] broken
// [if] modFxView rows lack effect, beats, cycle_ms, tempo_bpm, lfo_phase or inert_reason [then] broken
// [if] the poll timer runs with no live instance [then] broken
import assert from 'node:assert/strict';
import { after, before, beforeEach, describe, mock, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let live;
let lfo;
let player;

/** A 120 BPM grid (0.5 s per beat) from 0.25 s; the first downbeat is grid index 2. */
function grid120(count = 600, withDownbeat = true) {
	return Array.from({ length: count }, (_unused, i) => ({
		n: withDownbeat ? ((i + 2) % 4) + 1 : 2,
		bpm: 120,
		t: 0.25 + i * 0.5
	}));
}

function fakePort({ effect = 'flanger', cycleBeats = 4, timeMs = null, time = 10 } = {}) {
	return {
		effect,
		cycleBeats,
		timeMs,
		context: { currentTime: time },
		posted: [],
		crossfades: 0,
		postAnchor(anchor) {
			this.posted.push(anchor);
		},
		crossfadeWet() {
			this.crossfades += 1;
		}
	};
}

function setDeck(
	deck,
	{ beats = grid120(), position_ms = 20000, playing = true, pitch = 1, bpm = 120 } = {}
) {
	const st = player.deckStates[deck];
	st.anlz = beats === null ? null : { beatgrid: { beats } };
	st.position_ms = position_ms;
	st.playing = playing;
	st.pitch = pitch;
	st.bpm = bpm;
}

/** Advance the context clock and the playhead together by `sec` (no jump). */
function advance(deck, port, sec) {
	port.context.currentTime += sec;
	const st = player.deckStates[deck];
	st.position_ms += sec * 1000 * (st.playing ? st.pitch : 0);
}

before(async () => {
	({ live, lfo, player } = await loadTypeScriptModule('tests/unit/fixtures/mod-fx-entry.ts', {
		viteApiBase: 'http://127.0.0.1:9'
	}));
});
beforeEach(() => live._resetModFxForTests());
after(() => live._resetModFxForTests());

describe('mod fx live anchor', () => {
	test('engage posts the lfoAnchor anchor at once', () => {
		setDeck(1);
		const port = fakePort();
		live.attachModFx(1, port);
		assert.equal(port.posted.length, 1);
		assert.deepEqual(port.posted[0], lfo.lfoAnchor(grid120(), 20, 120, 10, 4));
		const [row] = live.modFxView();
		assert.equal(row.tempo_bpm, 120);
		assert.equal(row.cycle_ms, 2000);
		assert.ok(row.lfo_phase >= 0 && row.lfo_phase < 1);
		assert.equal(row.inert_reason, null);
	});

	test('tempo law: 0.05 BPM threshold, no phase jump', () => {
		setDeck(1);
		const port = fakePort();
		live.attachModFx(1, port);
		player.deckStates[1].pitch = 1.0003; // 120.036 BPM
		advance(1, port, 0.25);
		live._tickModFxForTests();
		assert.equal(port.posted.length, 1);
		player.deckStates[1].pitch = 1.0005; // 120.06 BPM
		advance(1, port, 0.25);
		live._tickModFxForTests();
		assert.equal(port.posted.length, 2);
		const [oldA, newA] = port.posted;
		const now = port.context.currentTime;
		assert.ok(Math.abs(newA.beatsPerSec - 120.06 / 60) < 1e-6);
		assert.ok(Math.abs(lfo.lfoPhase(oldA, now) - lfo.lfoPhase(newA, now)) < 1e-9);
		assert.equal(port.crossfades, 0);
	});

	test('a playhead jump over 50 ms re-locks with a wet crossfade', () => {
		setDeck(1);
		const port = fakePort();
		live.attachModFx(1, port);
		advance(1, port, 0.25);
		live._tickModFxForTests();
		assert.equal(port.posted.length, 1);
		advance(1, port, 0.25);
		player.deckStates[1].position_ms += 40;
		live._tickModFxForTests();
		assert.equal(port.posted.length, 1);
		advance(1, port, 0.25);
		player.deckStates[1].position_ms += 2000;
		live._tickModFxForTests();
		assert.equal(port.posted.length, 2);
		assert.equal(port.crossfades, 1);
		assert.deepEqual(
			port.posted[1],
			lfo.lfoAnchor(
				grid120(),
				player.deckStates[1].position_ms / 1000,
				120,
				port.context.currentTime,
				4
			)
		);
	});

	test('inert reasons, time_ms mode, and a late grid locks', () => {
		setDeck(1, { beats: null, bpm: null });
		const noGrid = fakePort();
		live.attachModFx(1, noGrid);
		setDeck(2, { beats: grid120(600, false) });
		live.attachModFx(2, fakePort());
		setDeck(3, { pitch: 0.1 }); // 12 BPM
		live.attachModFx(3, fakePort());
		setDeck(4, { beats: null, bpm: null });
		const timed = fakePort({ effect: 'phaser', timeMs: 500 });
		live.attachModFx(4, timed);
		const rows = live.modFxView();
		assert.equal(rows[0].inert_reason, live.MOD_FX_NO_GRID_REASON);
		assert.equal(rows[0].lfo_phase, null);
		assert.equal(rows[0].cycle_ms, null);
		assert.equal(noGrid.posted.length, 0);
		assert.equal(rows[1].inert_reason, live.MOD_FX_NO_DOWNBEAT_REASON);
		assert.equal(rows[2].inert_reason, live.MOD_FX_NO_TEMPO_REASON);
		assert.equal(rows[3].inert_reason, null);
		assert.equal(rows[3].cycle_ms, 500);
		assert.equal(rows[3].beats, null);
		assert.equal(timed.posted.length, 1);
		assert.equal(timed.posted[0].kind, 'time');
		setDeck(1);
		live._tickModFxForTests();
		assert.equal(noGrid.posted.length, 1);
		assert.equal(live.modFxView()[0].inert_reason, null);
	});

	test('the tempo vanishing keeps the last anchor running', () => {
		setDeck(1);
		const port = fakePort();
		live.attachModFx(1, port);
		player.deckStates[1].anlz = null;
		player.deckStates[1].bpm = null;
		advance(1, port, 0.25);
		live._tickModFxForTests();
		assert.equal(port.posted.length, 1);
		const [row] = live.modFxView();
		assert.equal(row.tempo_bpm, 120);
		assert.equal(row.inert_reason, null);
		assert.equal(typeof row.lfo_phase, 'number');
	});

	test('view shape, one row per instance', () => {
		setDeck(1);
		setDeck(2);
		live.attachModFx(1, fakePort());
		live.attachModFx(2, fakePort({ effect: 'phaser' }));
		const rows = live.modFxView();
		assert.equal(rows.length, 2);
		assert.deepEqual(Object.keys(rows[0]).sort(), [
			'beats',
			'cycle_ms',
			'deck',
			'effect',
			'id',
			'inert_reason',
			'lfo_phase',
			'tempo_bpm'
		]);
		assert.equal(rows[1].effect, 'phaser');
	});

	test('the poll runs only while an instance is live', () => {
		setDeck(1);
		assert.equal(live._modFxPollLiveForTests(), false);
		const a = live.attachModFx(1, fakePort());
		const b = live.attachModFx(1, fakePort());
		assert.equal(live._modFxPollLiveForTests(), true);
		a.detach();
		assert.equal(live._modFxPollLiveForTests(), true);
		b.detach();
		b.detach();
		assert.equal(live._modFxPollLiveForTests(), false);
	});

	test('no leaked interval after the last detach', () => {
		mock.timers.enable({ apis: ['setInterval'] });
		try {
			setDeck(1);
			const port = fakePort();
			const handle = live.attachModFx(1, port);
			player.deckStates[1].position_ms += 5000;
			mock.timers.tick(250);
			assert.equal(port.posted.length, 2); // the interval did fire while live
			handle.detach();
			player.deckStates[1].position_ms += 5000;
			mock.timers.tick(1000);
			assert.equal(port.posted.length, 2);
		} finally {
			mock.timers.reset();
		}
	});

	test('fail fast on a bad deck or port', () => {
		assert.throws(() => live.attachModFx(9, fakePort()), /deck/);
		const port = fakePort();
		delete port.postAnchor;
		assert.throws(() => live.attachModFx(1, port), /postAnchor/);
		assert.equal(live._modFxPollLiveForTests(), false);
	});
});
