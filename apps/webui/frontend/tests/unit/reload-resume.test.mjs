/**
 * RESCUE-07: a /performance reload that stopped playing decks never lands
 * silently stopped.
 *
 * Mon 5 Oct 2026 19:59:25Z, silver preview: a hard reload mid-play (deck 1
 * master, AutoPlay ON) kept the position but left every deck stopped, no master,
 * no prompt and no log line. RESCUE-02 resume is Gig-posture only and the
 * browser will not start audio before a click, so the honest answer is a
 * visible one-click Resume plus a grep-stable log line.
 *
 * [if] the session snapshot says decks were playing and the reload stopped them
 *   [then] a Resume offer names them, master first [⛔️ if the page is silent].
 * [if] the snapshot is older than 10 min, or nothing was playing [then] no offer
 *   [⛔️ if a stale set is offered as if it was just interrupted].
 * [if] the offer is accepted [then] each deck gets an ordinary `play`, master
 *   first [⛔️ if decks start with no master].
 */
import assert from 'node:assert/strict';
import { before, mock, test } from 'node:test';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

let plan;
let snap;
let holder;
let session;

const NOW = 1_800_000_000_000;

function _deck(overrides = {}) {
	return {
		stable_id: null,
		position_ms: 0,
		pitch: 1,
		pitch_range: 8,
		quantize_enabled: true,
		beat_sync_enabled: false,
		master_tempo_enabled: true,
		key_sync_enabled: false,
		playing: false,
		...overrides
	};
}

function _channel() {
	return { trim: 0.5, eq_high: 0.5, eq_mid: 0.5, eq_low: 0.5, filter: 0.5, fader: 1, assign: 'THRU' };
}

function _stems() {
	return {
		vocal: { muted: false, solo: false, gain: 0.5 },
		instrumental: { muted: false, solo: false, gain: 0.5 },
		drums: { muted: false, solo: false, gain: 0.5 }
	};
}

function _snapshotInput(overrides = {}) {
	return {
		captured_at_ms: NOW - 5_000,
		playlist_id: null,
		master_deck: 2,
		decks: {
			1: _deck({ stable_id: 'sid-a', position_ms: 107_800, playing: true }),
			2: _deck({ stable_id: 'sid-b', position_ms: 30_000, playing: true }),
			3: _deck({ stable_id: 'sid-c', position_ms: 0, playing: false }),
			4: _deck()
		},
		mixer: { crossfader: 0.5, master: 0.8, channels: { 1: _channel(), 2: _channel(), 3: _channel(), 4: _channel() } },
		stems: { 1: _stems(), 2: _stems(), 3: _stems(), 4: _stems() },
		...overrides
	};
}

function _stopped(ids) {
	const decks = {};
	for (const deckId of [1, 2, 3, 4]) {
		decks[deckId] = { stable_id: ids[deckId] ?? null, playing: false };
	}
	return decks;
}

before(async () => {
	plan = await loadTypeScriptModule('src/lib/rb/reload-resume.ts');
	snap = await loadTypeScriptModule('src/lib/rb/performance-session-snapshot.ts');
	holder = await loadTypeScriptModule('src/lib/rb/reload-resume.svelte.ts');
	session = await loadTypeScriptModule('src/lib/rb/performance-session.svelte.ts');
});

test('[RESCUE-07] the session snapshot keeps playing and master_deck; older ones read as stopped', () => {
	const parsed = snap.parsePerformanceSession(snap.serializePerformanceSession(_snapshotInput()));
	assert.equal(parsed.master_deck, 2);
	assert.equal(parsed.decks[1].playing, true);
	assert.equal(parsed.decks[3].playing, false);

	const legacy = JSON.parse(snap.serializePerformanceSession(_snapshotInput()));
	delete legacy.master_deck;
	for (const deckId of [1, 2, 3, 4]) delete legacy.decks[deckId].playing;
	const old = snap.parsePerformanceSession(JSON.stringify(legacy));
	assert.notEqual(old, null, 'a snapshot from before RESCUE-07 still parses');
	assert.equal(old.master_deck, null);
	assert.equal(old.decks[1].playing, false);

	const bad = JSON.parse(snap.serializePerformanceSession(_snapshotInput()));
	bad.master_deck = 7;
	assert.equal(snap.parsePerformanceSession(JSON.stringify(bad)), null);
});

test('[RESCUE-07] planReloadResumeOffer offers the stopped decks that were playing, master first', () => {
	const snapshot = snap.parsePerformanceSession(snap.serializePerformanceSession(_snapshotInput()));
	const offer = plan.planReloadResumeOffer({
		snapshot,
		decks: _stopped({ 1: 'sid-a', 2: 'sid-b', 3: 'sid-c' }),
		now_ms: NOW
	});
	assert.deepEqual(offer.decks, [2, 1]);
	assert.match(
		plan.reloadResumeOfferMessage(offer, true),
		/^\[reload-resume\] 2 deck\(s\) \(2,1\) were playing before reload and are stopped; click Resume to start them; AutoPlay is ON/
	);
});

test('[RESCUE-07] no offer when stale, when nothing was playing, or when the track did not come back', () => {
	const snapshot = snap.parsePerformanceSession(snap.serializePerformanceSession(_snapshotInput()));
	const decks = _stopped({ 1: 'sid-a', 2: 'sid-b' });
	assert.equal(
		plan.planReloadResumeOffer({ snapshot, decks, now_ms: NOW + plan.RELOAD_RESUME_WINDOW_MS }),
		null
	);
	assert.equal(plan.planReloadResumeOffer({ snapshot: null, decks, now_ms: NOW }), null);
	const idle = snap.parsePerformanceSession(
		snap.serializePerformanceSession(
			_snapshotInput({
				decks: { 1: _deck({ stable_id: 'sid-a' }), 2: _deck(), 3: _deck(), 4: _deck() }
			})
		)
	);
	assert.equal(plan.planReloadResumeOffer({ snapshot: idle, decks, now_ms: NOW }), null);
	const onlyOne = plan.planReloadResumeOffer({
		snapshot,
		decks: _stopped({ 1: 'sid-a', 2: 'other-track' }),
		now_ms: NOW
	});
	assert.deepEqual(onlyOne.decks, [1], 'a deck that reloaded a different track is not offered');
	const alreadyPlaying = plan.planReloadResumeOffer({
		snapshot,
		decks: { ..._stopped({ 1: 'sid-a' }), 2: { stable_id: 'sid-b', playing: true } },
		now_ms: NOW
	});
	assert.deepEqual(alreadyPlaying.decks, [1]);
});

test('[RESCUE-07] accepting plays each offered deck in order, master first', async () => {
	holder.offerReloadResume(null, false);
	const warn = mock.method(console, 'warn', () => {});
	try {
		holder.offerReloadResume({ decks: [2, 1], captured_at_ms: NOW }, true);
	} finally {
		warn.mock.restore();
	}
	const commands = [];
	const started = await holder.acceptReloadResume(async (command) => {
		commands.push(command);
	});
	assert.deepEqual(started, [2, 1]);
	assert.deepEqual(commands, [
		{ type: 'play', deck: 2, playing: true },
		{ type: 'play', deck: 1, playing: true }
	]);
	assert.equal(holder.reloadResume.offer, null);
});

test('[RESCUE-07] a refused play keeps the offer up with the reason', async () => {
	const warn = mock.method(console, 'warn', () => {});
	const error = mock.method(console, 'error', () => {});
	try {
		holder.offerReloadResume({ decks: [1, 2], captured_at_ms: NOW }, false);
		const started = await holder.acceptReloadResume(async (command) => {
			if (command.deck === 2) throw new Error('deck 2 is not loaded');
		});
		assert.deepEqual(started, [1]);
		assert.notEqual(holder.reloadResume.offer, null);
		assert.match(holder.reloadResume.error, /deck 2: deck 2 is not loaded/);
	} finally {
		warn.mock.restore();
		error.mock.restore();
		holder.dismissReloadResume();
	}
});

// REQ: RESCUE-07
test('[RESCUE-07] RUNNING it: a reload restore with playing decks logs the Resume offer', async () => {
	const raw = snap.serializePerformanceSession(_snapshotInput());
	const store = new Map([['mdt.rb.performance-session.v1', raw]]);
	const loaded = { 1: null, 2: null, 3: null, 4: null };
	const warnings = [];
	const warn = mock.method(console, 'warn', (message) => warnings.push(String(message)));
	const query = () => {
		const decks = {};
		for (const deckId of [1, 2, 3, 4]) {
			decks[deckId] = {
				..._deck({ stable_id: loaded[deckId] }),
				stems: { available_controls: ['vocal', 'instrumental', 'drums'], controls: _stems() }
			};
		}
		return {
			browser: { active_playlist: null },
			master_deck: null,
			mixer: { crossfader: 0.5, master: 0.8, channels: { 1: _channel(), 2: _channel(), 3: _channel(), 4: _channel() } },
			decks
		};
	};
	let dispose = () => {};
	try {
		dispose = session.installPerformanceSessionRestore({
			location: { pathname: '/performance', search: '', href: 'http://x/performance' },
			storage: { getItem: (key) => store.get(key) ?? null, setItem: (key, value) => store.set(key, value) },
			replaceState: () => {},
			commandSession: () => 1,
			operatorMaster: () => null,
			now: () => NOW,
			autoPlayEnabled: () => true,
			document: { hidden: false, addEventListener: () => {}, removeEventListener: () => {} },
			window: { addEventListener: () => {}, removeEventListener: () => {} },
			setInterval: () => 0,
			clearInterval: () => {},
			dispatch: async (command) => {
				if (command.type === 'load') loaded[command.deck] = command.stable_id;
			},
			query
		});
		for (let i = 0; i < 50 && !warnings.some((w) => w.startsWith('[reload-resume]')); i += 1) {
			await new Promise((resolve) => setTimeout(resolve, 10));
		}
	} finally {
		dispose();
		warn.mock.restore();
	}
	const line = warnings.find((w) => w.startsWith('[reload-resume]'));
	assert.ok(line, `no [reload-resume] line among: ${JSON.stringify(warnings)}`);
	assert.match(line, /2 deck\(s\) \(2,1\) were playing before reload/);
	assert.match(line, /AutoPlay is ON/);
});

test('[RESCUE-07] a shared-instant start (Gig rescue) still claims a master', () => {
	const source = readFileSync(
		fileURLToPath(new URL('../../src/lib/rb/audio-engine.svelte.ts', import.meta.url)),
		'utf8'
	);
	const branch = source.match(/if \(startAtContextSec !== undefined\) \{([\s\S]*?)\n\t\t\}/);
	assert.ok(branch, 'play() keeps its startAtContextSec branch');
	assert.match(branch[1], /_electPlayingMaster\(\{ reason: 'play-claim' \}\)/);
});

// REQ: RESCUE-07
test('[RESCUE-07] RUNNING it: a remount over a live engine restores nothing and loads onto no deck', async () => {
	const raw = snap.serializePerformanceSession(_snapshotInput());
	const store = new Map([['mdt.rb.performance-session.v1', raw]]);
	const commands = [];
	const infos = [];
	const info = mock.method(console, 'info', (message) => infos.push(String(message)));
	const live = { 1: 'sid-a', 2: 'sid-b', 3: null, 4: null };
	const query = () => {
		const decks = {};
		for (const deckId of [1, 2, 3, 4]) {
			decks[deckId] = {
				..._deck({ stable_id: live[deckId], playing: deckId === 2 }),
				stems: { available_controls: ['vocal', 'instrumental', 'drums'], controls: _stems() }
			};
		}
		return {
			browser: { active_playlist: null },
			master_deck: 2,
			mixer: { crossfader: 0.5, master: 0.8, channels: { 1: _channel(), 2: _channel(), 3: _channel(), 4: _channel() } },
			decks
		};
	};
	let dispose = () => {};
	try {
		dispose = session.installPerformanceSessionRestore({
			location: { pathname: '/performance', search: '', href: 'http://x/performance' },
			storage: { getItem: (key) => store.get(key) ?? null, setItem: (key, value) => store.set(key, value) },
			replaceState: () => {},
			commandSession: () => 1,
			operatorMaster: () => null,
			now: () => NOW,
			autoPlayEnabled: () => true,
			document: { hidden: false, addEventListener: () => {}, removeEventListener: () => {} },
			window: { addEventListener: () => {}, removeEventListener: () => {} },
			setInterval: () => 0,
			clearInterval: () => {},
			dispatch: async (command) => {
				commands.push(command);
			},
			query
		});
		for (let i = 0; i < 50 && !infos.some((m) => m.startsWith('[session-restore]')); i += 1) {
			await new Promise((resolve) => setTimeout(resolve, 10));
		}
	} finally {
		dispose();
		info.mock.restore();
	}
	assert.deepEqual(commands, [], 'no load, seek, tempo or mixer command replayed over live decks');
	assert.ok(infos.some((m) => /skipped: deck\(s\) 1,2 already loaded/.test(m)), JSON.stringify(infos));
	assert.deepEqual(session.liveDecksAtRestore({ 1: { stable_id: null }, 2: { stable_id: 'x' }, 3: { stable_id: null }, 4: { stable_id: null } }), [2]);
});
