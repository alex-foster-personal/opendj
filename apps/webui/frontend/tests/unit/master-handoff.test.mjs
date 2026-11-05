/**
 * MASTER handoff by what the room hears (the maintainer, Tue 27 Oct 2026).
 *
 * Before: a MASTER click locked, the lock blocked every automatic handoff
 * (even a stop or a play-out), and only exact gain silence (< 1e-4) of trim,
 * fader, crossfader and master counted, so EQ kills, filter sweeps and stem
 * mutes never moved the master and play/pause never re-checked.
 *
 * [if] a master killed on all three EQ bands stays master while another deck plays loud [then] broken.
 * [if] a THRU master loses MASTER because the crossfader is parked on the other side [then] broken.
 * [if] a faded-out master does not hand off once another deck starts playing [then] play/pause never re-checks.
 * [if] a dip shorter than one beat hands off [then] every breakdown flips MASTER.
 * [if] a LOCKED master moves on level, or a stopped LOCKED master keeps MASTER [then] broken.
 * [if] a plain MASTER click sends lock:true [then] picking a deck turns AUTO off again.
 */
import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { readFrontendSource } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let h;
let election;
before(async () => {
	h = await loadTypeScriptModule('src/lib/rb/master-handoff.ts');
	election = await loadTypeScriptModule('src/lib/rb/master-election.ts');
});

const STEMS_OFF = { status: 'unavailable', layout: null, controls: null };

function stemControls(mutedAll) {
	const c = {};
	for (const id of ['vocal', 'instrumental', 'drums', 'bass', 'other'])
		c[id] = { muted: mutedAll, solo: false, gain: 0.5 };
	return c;
}

function channel(over = {}) {
	return {
		trim: 0.5,
		fader: 1,
		assign: 'A',
		eq_low: 0.5,
		eq_mid: 0.5,
		eq_high: 0.5,
		filter: 0.5,
		...over
	};
}

/** Deck 1 on A, deck 2 on B, crossfader centred, everything at unity. */
function state({ ch1 = {}, ch2 = {}, mixer = {}, d1 = {}, d2 = {} } = {}) {
	return {
		decks: {
			1: { audible: true, stems: STEMS_OFF, ...d1 },
			2: { audible: true, stems: STEMS_OFF, ...d2 },
			3: { audible: false, stems: STEMS_OFF },
			4: { audible: false, stems: STEMS_OFF }
		},
		mixer: {
			crossfader: 0.5,
			master: 1,
			headphones: {},
			channels: {
				1: channel(ch1),
				2: channel({ assign: 'B', ...ch2 }),
				3: channel(),
				4: channel()
			},
			...mixer
		}
	};
}

const db = (s, deck) => h.heardLevelDb(s, deck, false, new Set());

function decksFrom(s, playing = { 1: true, 2: true }) {
	return [1, 2, 3, 4].map((id) => ({ id, playing: playing[id] === true, levelDb: db(s, id) }));
}

/** Run the hold over `ms` in 100 ms ticks; returns the deck handed to, or null. */
function runHold(decks, { ms, bpm = 120, locked = false, master = 1 }) {
	let st = h.LEVEL_HANDOFF_IDLE;
	for (let t = 0; t <= ms; t += h.HANDOFF_TICK_MS) {
		const step = h.stepLevelHandoff(st, { master, locked, masterBpm: bpm, decks, nowMs: t });
		if (step.handoffTo !== null) return { to: step.handoffTo, at: t };
		st = step.state;
	}
	return null;
}

describe('heard level after EQ, filter and stems', () => {
	it('unity channel reads 0 dB at centre crossfader (-3 dB equal power)', () => {
		assert.ok(Math.abs(db(state(), 1) - -3.01) < 0.05, String(db(state(), 1)));
	});
	it('EQ kill on all three bands is silent; one band killed is not', () => {
		assert.equal(db(state({ ch1: { eq_low: 0, eq_mid: 0, eq_high: 0 } }), 1), -Infinity);
		assert.ok(db(state({ ch1: { eq_low: 0 } }), 1) > h.CANDIDATE_LOUD_DB);
	});
	it('the filter counts: a closed LP/HP removes the octaves it cuts, enough to tip a low fader under -30 dB', () => {
		assert.equal(h.filterPowerShare(0.5), 1);
		assert.ok(h.filterPowerShare(0.25) < 1 && h.filterPowerShare(0) < h.filterPowerShare(0.25));
		assert.ok(h.filterPowerShare(1) < h.filterPowerShare(0.75));
		const low = { fader: 0.06 };
		assert.ok(
			db(state({ ch1: low }), 1) > h.MASTER_QUIET_DB,
			'control: the fader alone is above -30 dB'
		);
		assert.ok(
			db(state({ ch1: { ...low, filter: 0 } }), 1) < h.MASTER_QUIET_DB,
			'the closed filter is not counted'
		);
	});
	it('every stem muted is silent', () => {
		const muted = { status: 'ready', layout: 'demucs4', controls: stemControls(true) };
		assert.equal(db(state({ d1: { stems: muted } }), 1), -Infinity);
		const open = { status: 'ready', layout: 'demucs4', controls: stemControls(false) };
		assert.ok(db(state({ d1: { stems: open } }), 1) > h.CANDIDATE_LOUD_DB);
		// if a ready layout the stem graph cannot resolve reads as unity then a broken stem contract can hold MASTER
		const empty = { status: 'ready', layout: 'no-such-layout', controls: stemControls(false) };
		assert.throws(() => db(state({ d1: { stems: empty } }), 1));
	});
	it('THRU ignores the crossfader, as the graph wires it; A does not', () => {
		const parkedOnB = state({ ch1: { assign: 'THRU' }, mixer: { crossfader: 1 } });
		assert.ok(db(parkedOnB, 1) > h.CANDIDATE_LOUD_DB);
		assert.ok(db(state({ mixer: { crossfader: 1 } }), 1) < h.MASTER_QUIET_DB);
	});
	it('the master mute and a deck not presenting audio are silent', () => {
		assert.equal(h.heardLevelDb(state(), 1, true, new Set()), -Infinity);
		assert.equal(db(state({ d1: { audible: false } }), 1), -Infinity);
	});
});

describe('level handoff: below -30 dB for one beat while another playing deck is above -20 dB', () => {
	it('EQ kill: hands off after one beat (500 ms at 120 BPM), not before', () => {
		const decks = decksFrom(state({ ch1: { eq_low: 0, eq_mid: 0, eq_high: 0 } }));
		assert.deepEqual(runHold(decks, { ms: 2000 }), { to: 2, at: 500 });
		assert.equal(runHold(decks, { ms: 400 }), null);
	});
	it('one beat follows tempo: 174 BPM holds 345 ms; no tempo holds 500 ms', () => {
		assert.ok(Math.abs(h.handoffHoldMs(174) - 344.8) < 0.1);
		assert.equal(h.handoffHoldMs(null), h.HANDOFF_HOLD_FALLBACK_MS);
	});
	it('THRU: a THRU master with the crossfader on the other deck stays master', () => {
		const decks = decksFrom(state({ ch1: { assign: 'THRU' }, mixer: { crossfader: 1 } }));
		assert.equal(runHold(decks, { ms: 3000 }), null);
	});
	it('THRU: a THRU candidate takes over from a master crossfaded out', () => {
		const decks = decksFrom(state({ ch2: { assign: 'THRU' }, mixer: { crossfader: 1 } }));
		assert.equal(runHold(decks, { ms: 3000 }).to, 2);
	});
	it('play after fade: a faded master hands off once the other deck PLAYS, a beat after it starts', () => {
		const faded = state({ ch1: { fader: 0.01 } });
		let st = h.LEVEL_HANDOFF_IDLE;
		const paused = decksFrom(faded, { 1: true });
		for (let t = 0; t <= 2000; t += 100) {
			const step = h.stepLevelHandoff(st, {
				master: 1,
				locked: false,
				masterBpm: 120,
				decks: paused,
				nowMs: t
			});
			assert.equal(step.handoffTo, null, 'a paused candidate never takes MASTER');
			st = step.state;
		}
		const playing = decksFrom(faded);
		let handed = null;
		for (let t = 2000; t <= 4000 && handed === null; t += 100) {
			const step = h.stepLevelHandoff(st, {
				master: 1,
				locked: false,
				masterBpm: 120,
				decks: playing,
				nowMs: t
			});
			if (step.handoffTo !== null) handed = { to: step.handoffTo, at: t };
			st = step.state;
		}
		assert.deepEqual(handed, { to: 2, at: 2500 });
	});
	it('a dip that comes back before the beat ends resets the hold', () => {
		const quiet = decksFrom(state({ ch1: { fader: 0.01 } }));
		const loud = decksFrom(state());
		let st = h.LEVEL_HANDOFF_IDLE;
		const at = (decks, t) =>
			h.stepLevelHandoff(st, { master: 1, locked: false, masterBpm: 120, decks, nowMs: t });
		for (const [decks, t] of [
			[quiet, 0],
			[quiet, 300],
			[loud, 400],
			[quiet, 500],
			[quiet, 900]
		]) {
			const step = at(decks, t);
			assert.equal(step.handoffTo, null, `t=${t}`);
			st = step.state;
		}
		assert.equal(at(quiet, 1000).handoffTo, 2);
	});
	it('a candidate at or below -20 dB is not loud enough; the loudest of two wins', () => {
		const s = state({ ch1: { fader: 0.01 }, ch2: { fader: 0.05 } });
		assert.ok(db(s, 2) <= h.CANDIDATE_LOUD_DB);
		assert.equal(runHold(decksFrom(s), { ms: 3000 }), null);
		const three = [
			...decksFrom(state({ ch1: { fader: 0.01 } })).slice(0, 2),
			{ id: 3, playing: true, levelDb: 0 },
			{ id: 4, playing: false, levelDb: 0 }
		];
		assert.equal(runHold(three, { ms: 3000 }).to, 3);
	});
	it('a LOCKED master never moves on level', () => {
		const decks = decksFrom(state({ ch1: { eq_low: 0, eq_mid: 0, eq_high: 0 } }));
		assert.equal(runHold(decks, { ms: 5000, locked: true }), null);
	});
	it('a decided handoff is decided again by the same hold when its scheduler turn comes', () => {
		const quiet = decksFrom(state({ ch1: { fader: 0.01 } }));
		const loud = decksFrom(state());
		const step = (st, decks, nowMs) =>
			h.stepLevelHandoff(st, { master: 1, locked: false, masterBpm: 120, decks, nowMs });
		const fired = runHold(quiet, { ms: 3000 });
		let st = h.LEVEL_HANDOFF_IDLE;
		for (let t = 0; t <= fired.at; t += h.HANDOFF_TICK_MS) st = step(st, quiet, t).state;
		// if the dip is unbroken then the queued handoff still goes ahead at its turn
		assert.equal(step(st, quiet, fired.at + 300).handoffTo, 2);
		// if the master recovers and dips again while queued then the new dip needs its own full beat
		const recovered = step(st, loud, fired.at + 100).state;
		const redip = step(recovered, quiet, fired.at + 200).state;
		assert.equal(step(redip, quiet, fired.at + 300).handoffTo, null);
		// if another deck is louder by the time the turn comes then that deck wins
		const three = [
			...quiet.slice(0, 2),
			{ id: 3, playing: true, levelDb: 3 },
			{ id: 4, playing: false, levelDb: 0 }
		];
		assert.equal(step(st, three, fired.at + 300).handoffTo, 3);
		// if the target stopped then nothing is handed off
		assert.equal(
			step(st, decksFrom(state({ ch1: { fader: 0.01 } }), { 1: true }), fired.at + 300).handoffTo,
			null
		);
	});

	it('a corrupt tempo or a ready stem state with no layout throws instead of reading as plausible', () => {
		// if a zero, negative or NaN tempo silently gets the 500 ms fallback then corrupt state times the handoff
		for (const bad of [0, -120, NaN, Infinity])
			assert.throws(() => h.handoffHoldMs(bad), /not a BPM/);
		assert.equal(h.handoffHoldMs(null), 500);
		// if ready stems with no layout read as the full mix then a broken stem contract can hold MASTER
		const noLayout = { status: 'ready', layout: null, controls: stemControls(false) };
		assert.throws(() => db(state({ d1: { stems: noLayout } }), 1), /no layout/);
	});
	it('a NaN or +Infinity level throws instead of reading as silence', () => {
		// if a broken level formats as -inf then the cause hides behind a silent-looking message
		assert.throws(
			() => h.masterHandoffMessage(1, 2, 'master-left', { masterDb: NaN, toDb: 0 }),
			/not a dB value/
		);
		assert.throws(
			() => h.masterHandoffMessage(1, 2, 'master-left', { masterDb: -40, toDb: Infinity }),
			/not a dB value/
		);
	});
	it('a failed queued level handoff is surfaced as an error toast, not only logged', () => {
		// if the failure is only logged then the stale master stays with nothing telling the DJ
		const seen = [];
		election.reportLevelHandoffFailure(
			1,
			new Error('boom'),
			(...a) => seen.push(['event', ...a]),
			(...a) => seen.push(['toast', ...a])
		);
		assert.deepEqual(
			seen.map((r) => r[0]),
			['event', 'toast']
		);
		assert.equal(seen[1][1], 'MASTER handoff from deck 1 failed: boom');
		assert.match(
			readFrontendSource('src/lib/rb/audio-engine.svelte.ts'),
			/reportLevelHandoffFailure\(master, e, recordPerfEvent, pushToast\)/
		);
	});

	it('the handoff message names both decks and both levels', () => {
		assert.equal(
			h.masterHandoffMessage(1, 2, 'master-left', { masterDb: -Infinity, toDb: -3.0103 }),
			'master 1 -> 2 (master-left): master -inf dB, deck 2 -3.0 dB'
		);
		assert.equal(h.masterHandoffMessage(2, null, 'unload'), 'master 2 -> none (unload)');
	});
});

describe('MASTER button: click picks in AUTO, the lock gesture locks', () => {
	const auto1 = { master_deck: 1, master_mode: 'auto' };
	const locked1 = { master_deck: 1, master_mode: 'locked' };
	it('a plain click never locks', () => {
		for (const cur of [auto1, locked1, { master_deck: null, master_mode: 'auto' }]) {
			for (const deck of [1, 2])
				assert.deepEqual(election.masterButtonCommand(cur, deck, false), {
					type: 'master',
					deck,
					lock: false
				});
		}
	});
	it('long-press or Shift/Alt-click locks, and on the locked master unlocks', () => {
		assert.equal(election.masterButtonCommand(auto1, 1, true).lock, true);
		assert.equal(election.masterButtonCommand(auto1, 2, true).lock, true);
		assert.equal(election.masterButtonCommand(locked1, 1, true).lock, false);
		assert.equal(election.masterButtonCommand(locked1, 2, true).lock, true);
	});
	it('a repeated plain click on the paused master keeps it in AUTO in both engines', () => {
		// if the same-master shortcut re-elects then MASTER goes to null while nothing plays
		const manual = readFrontendSource('src/lib/rb/audio-engine.svelte.ts');
		assert.match(
			manual,
			/if \(options\?\.lock === false && _masterDeck === deck\) \{\s*\/\/[^\n]*\n\s*_masterMode = 'auto';\s*return;/
		);
		const rust = readFrontendSource('src/lib/audio-engine/rust-transport.ts');
		assert.match(
			rust,
			/if \(lock === false && rustMaster\.deck === deck\) \{\s*rustMaster\.mode = 'auto';\s*return;/
		);
	});
	it('the header wires click, Shift/Alt and a long-press to it, and Deck sends its command', () => {
		const header = readFrontendSource('src/lib/components/rb/deck/DeckHeader.svelte');
		assert.match(header, /await onMaster\(event\.shiftKey \|\| event\.altKey\)/);
		assert.match(
			header,
			/setTimeout\(\(\) => \{[\s\S]*?void onMaster\(true\);[\s\S]*?\}, MASTER_LOCK_LONG_PRESS_MS\)/
		);
		// if a cancelled gesture or a secondary button can arm the hold then the lock flips unasked
		assert.match(header, /onpointercancel=\{cancelMasterHold\}/);
		assert.match(header, /if \(!event\.isPrimary \|\| event\.button !== 0\) return;/);
		assert.match(
			header,
			/class:locked=\{deck\.is_master && masterMode === 'locked'\}/,
			'the LOCK badge is gone'
		);
		const deck = readFrontendSource('src/lib/components/rb/Deck.svelte');
		assert.match(deck, /masterButtonCommand\(queryPerformanceState\(\), deckId, lockGesture\)/);
	});
});

describe('the engine: stop, end and unload always hand off; every change re-checks', () => {
	const engine = () => readFrontendSource('src/lib/rb/audio-engine.svelte.ts');
	function body(src, sig) {
		const start = src.indexOf(sig);
		assert.ok(start >= 0, `${sig} not found`);
		return src.slice(start, src.indexOf('\n}\n', start));
	}
	it('locked stop: a master that stops or plays out releases the lock and is forced', () => {
		const src = engine();
		const stopped = body(src, 'function _handOffStoppedMaster(');
		assert.match(stopped, /if \(_masterMode === 'locked'\) _masterMode = 'auto';/);
		assert.match(stopped, /_electPlayingMaster\(\{ force: true, reason \}\)/);
		const transition = body(src, 'function _handleAudibleTransition(');
		assert.match(
			transition,
			/if \(_masterDeck === deck && !st\.playing\) \{\s*_handOffStoppedMaster\('master-left'\);/
		);
		assert.doesNotMatch(
			transition.split('_handOffStoppedMaster')[0],
			/_masterMode !== 'auto'\) return/,
			'a lock still blocks the stop handoff'
		);
		assert.match(
			src,
			/if \(_masterDeck === deck && !st\.playing\) _handOffStoppedMaster\('natural-end'\);/
		);
	});
	it('re-checks on play/pause, EQ, filter, stems, the mixer and a 100 ms tick', () => {
		const src = engine();
		assert.match(body(src, 'function _handleAudibleTransition('), /_maybeHandoffOnAir\(\);/);
		assert.match(
			src,
			/logEqApply\(deck, pressT0Ms, now, PARAM_SMOOTH_S\); _maybeHandoffOnAir\(\);/
		);
		assert.match(src, /logMixerApply\(FILTER_APPLY_KIND[^\n]*\n\t\t_maybeHandoffOnAir\(\);/);
		assert.match(src, /'muted', muted, [^\n]*\); _maybeHandoffOnAir\(\);/);
		assert.match(src, /'solo', solo, [^\n]*\); _maybeHandoffOnAir\(\);/);
		assert.match(
			src,
			/nowMs - _lastHandoffCheckMs >= HANDOFF_TICK_MS\) _maybeHandoffOnAir\(nowMs\)/
		);
	});
	it('the level election runs under the scheduler claim and re-validates there, never from the frame loop directly', () => {
		const onAir = body(engine(), 'function _maybeHandoffOnAir(');
		assert.match(onAir, /void runAutomaticMasterElection\(async \(\) => \{/);
		assert.match(onAir, /_masterDeck !== master \|\| _masterMode === 'locked'\) return;/);
		assert.match(onAir, /const due = _stepLevelHandoff\(performance\.now\(\)\);/);
		assert.equal((onAir.match(/_electPlayingMaster\(/g) ?? []).length, 1);
		assert.ok(
			onAir.indexOf('_electPlayingMaster(') > onAir.indexOf('runAutomaticMasterElection('),
			'master changes outside the claim'
		);
	});

	it('a manual MASTER command, even on the current master, restarts the hold and voids a queued handoff', () => {
		// if a click on the AUTO master keeps the old quiet clock then a queued handoff undoes the pick
		const src = engine();
		assert.match(
			body(src, 'async setDeckMaster('),
			/_levelHandoff = LEVEL_HANDOFF_IDLE;\s*_levelHandoffGeneration \+= 1;/
		);
		assert.match(
			body(src, 'function _maybeHandoffOnAir('),
			/if \(generation !== _levelHandoffGeneration \|\| _masterDeck !== master/
		);
	});
	it('every master change resets the level hold, and the extroute set is passed in, not defaulted', () => {
		// if a switch away and back keeps the old quiet clock then the deck hands off without a beat as master
		assert.match(body(engine(), 'function _assignMaster('), /_levelHandoff = LEVEL_HANDOFF_IDLE;/);
		// if the election reads location itself then a missing page context silently routes nothing
		const election = readFrontendSource('src/lib/rb/master-election.ts');
		assert.doesNotMatch(election, /location/);
		assert.match(election, /masterMuted: boolean,\s*routed: ReadonlySet<DeckId>/);
	});
	it('every master change is logged as a performance event', () => {
		assert.match(
			body(engine(), 'function _assignMaster('),
			/recordPerfEvent\('master-handoff', masterHandoffMessage\(/
		);
	});
});
