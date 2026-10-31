// requirement: CMDK-20
// requirement: CMDK-21
// requirement: CMDK-22
// requirement: CMDK-14
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * Cmd K slice 5 (#6093): Tab for AI search via JEV.
 *
 * Regression lines:
 * - if a played track, or one off the master's tempo or key, is sent to JEV then broken
 * - if half or double time is refused as a tempo that cannot mix then broken
 * - if the shortlist exceeds its size or lists a track twice then broken
 * - if a track JEV gave no number is ranked (guessed) instead of left out then broken
 * - if the ranking ignores JEV's probabilities or reorders ties then broken
 * - if Shift+Tab or a modified Tab flips the mode instead of cycling focus then broken
 * - if the bar calls JEV per keystroke, or below the minimum words, then broken
 * - if no key reads as "no matches" instead of the add-key row then broken
 * - if an answer for older words lands over newer ones then broken
 * - if the sweep keeps moving under reduced motion then broken
 * - if cmdk_mode takes anything but plain or ai then broken
 * - if a known-missing file is shortlisted, or a pending row (settled when picked) is dropped, then broken
 */

const SRC = new URL('../../src/', import.meta.url).pathname;
const FIXTURE = new URL('../../../../../tests/fixtures/jev/cmdk_ai_search.json', import.meta.url).pathname;
let ai;
let ipc;

before(async () => {
	ai = await loadTypeScriptModule('src/lib/rb/cmdk-ai.ts');
	ipc = await loadTypeScriptModule('tests/unit/fixtures/cmdk-ipc-entry.ts');
});

const ROW = (stable_id, bpm, key, extra = {}) => ({ stable_id, title: stable_id, artist: null, bpm, key, ...extra });

test('CMDK-20: the shortlist keeps unplayed tracks within the master tempo (or half or double) and one Camelot step', () => {
	const master = { bpm: 124, key: '8A' };
	const rows = [
		ROW('same', 124, '8A'),
		ROW('near', 128, '9A'),
		ROW('half', 62.5, '7A'),
		ROW('double', 247, '8B'),
		ROW('fast', 140, '8A'),
		ROW('far-key', 124, '11A'),
		ROW('no-bpm', null, '8A'),
		ROW('no-key', 124, null),
		ROW('played', 124, '8A'),
		ROW('same', 124, '8A')
	];
	const out = ai.shortlist(rows, master, new Set(['played'])).map((row) => row.stable_id);
	assert.deepEqual(out, ['same', 'near', 'half', 'double']);
	// No master: everything unplayed qualifies, still each once.
	assert.deepEqual(
		ai.shortlist(rows, null, new Set(['played'])).map((row) => row.stable_id),
		['same', 'near', 'half', 'double', 'fast', 'far-key', 'no-bpm', 'no-key']
	);
	// A master with no key lets every key through; one with no tempo every tempo.
	assert.ok(ai.keyFits('3B', null));
	assert.ok(ai.tempoFits(170, null));
	assert.equal(ai.tempoFits(124 * (1 + ai.BPM_TOLERANCE) + 0.1, 124), false);
	assert.equal(ai.tempoFits(124 * (1 + ai.BPM_TOLERANCE) - 0.1, 124), true);
});

test('CMDK-20: the shortlist stops at its size', () => {
	const rows = Array.from({ length: ai.SHORTLIST_SIZE + 20 }, (_, i) => ROW(`r${i}`, 124, '8A'));
	const out = ai.shortlist(rows, { bpm: 124, key: '8A' }, new Set());
	assert.equal(out.length, ai.SHORTLIST_SIZE);
	assert.equal(out[0].stable_id, 'r0');
	assert.equal(ai.shortlist(rows, null, new Set(), 3).length, 3);
});

test('CMDK-20: the recorded JEV answer ranks the vocal house track first and leaves unscored rows out', () => {
	const fx = JSON.parse(readFileSync(FIXTURE, 'utf8'));
	const scores = {};
	fx.tracks.forEach((track, i) => {
		scores[track.stable_id] = fx.response.answers[`t${i + 1}`].noul;
	});
	const rows = [...fx.tracks, ROW('unscored', 124, '8A')];
	const ranked = ai.rankByFit(rows, scores);
	assert.equal(ranked.length, fx.tracks.length, 'a row JEV gave no number is not guessed');
	assert.equal(ranked[0].row.stable_id, 'ai-03');
	for (let i = 1; i < ranked.length; i++) assert.ok(ranked[i - 1].p >= ranked[i].p);
	assert.equal(ai.fitLabel(ranked[0].p), `${Math.round(ranked[0].p * 100)}%`);
	// Ties keep the shortlist's order; NaN is not a number to rank by.
	const tied = ai.rankByFit([ROW('a', 1, null), ROW('b', 1, null), ROW('c', 1, null)], { a: 0.5, b: 0.5, c: Number.NaN });
	assert.deepEqual(
		tied.map((hit) => hit.row.stable_id),
		['a', 'b']
	);
});

test('CMDK-20: the shortlist leaves out a known-missing file and keeps a pending row', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const derived = bar.slice(bar.indexOf('const aiShortlist = $derived('), bar.indexOf('const aiRanked'));
	// false is missing; null is AVAILABILITY_PENDING, which the load path settles through rb-meta (CMDK-04).
	assert.match(derived, /collection\.filter\(\(row\) => row\.file_exists !== false\)/);
	const keep = (row) => row.file_exists !== false;
	assert.deepEqual([{ file_exists: true }, { file_exists: null }, { file_exists: false }].map(keep), [true, true, false]);
});

test('CMDK-20: what JEV is sent is the row text only', () => {
	assert.deepEqual(ai.aiTrack({ ...ROW('x', 120, '8A'), comments: 'secret', file_exists: true }), {
		stable_id: 'x',
		title: 'x',
		artist: null,
		album: null,
		genre: null,
		bpm: 120,
		key: '8A'
	});
});

test('CMDK-20: only a plain Tab flips the mode', () => {
	const none = { shift: false, meta: false, ctrl: false, alt: false };
	assert.equal(ai.nextMode('plain', 'Tab', none), 'ai');
	assert.equal(ai.nextMode('ai', 'Tab', none), 'plain');
	for (const mod of ['shift', 'meta', 'ctrl', 'alt']) assert.equal(ai.nextMode('plain', 'Tab', { ...none, [mod]: true }), null);
	assert.equal(ai.nextMode('plain', 'Enter', none), null);
});

test('CMDK-20: the bar flips the mode on Tab only with the flag on, before the focus cycle', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const keydown = bar.slice(bar.indexOf('function onInputKeydown'), bar.indexOf('function altShortcut'));
	const flip = keydown.indexOf('aiOn && nextMode(mode, event.key');
	const cycle = keydown.indexOf("event.key === 'Tab') {");
	assert.ok(flip > 0 && cycle > flip, 'the AI flip is checked before the plain Tab focus cycle');
	assert.match(bar, /const aiOn = \$derived\(cmdkAiEnabled\(\)\)/);
	// Closing the bar leaves AI mode and forgets the answer.
	const close = bar.slice(bar.indexOf('function closeBar'), bar.indexOf('function toggleFilter'));
	assert.match(close, /if \(mode !== 'plain'\) setMode\('plain'\)/);
	assert.match(bar, /if \(next === 'ai'\) checkCmdkAiKey\(\);\s*else resetCmdkAi\(\);/);
});

test('CMDK-20: plain Tab flips the mode from a focused icon or ring too, and hands focus to the input (Codex r4275264773)', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const keydown = bar.slice(bar.indexOf('function onWrapKeydown'));
	const flip = keydown.indexOf('aiOn && nextMode(mode, event.key');
	const cycle = keydown.indexOf("event.key === 'Tab') {");
	assert.ok(flip > 0 && cycle > flip, 'the AI flip is checked before the focus cycle');
	const branch = keydown.slice(flip, cycle);
	assert.match(branch, /setMode\(mode === 'plain' \? 'ai' : 'plain'\);\s*input\?\.focus\(\);/);
	// Shift+Tab and modified Tab never flip (nextMode answers null), so they still cycle focus.
	assert.equal(ai.nextMode('ai', 'Tab', { shift: true, meta: false, ctrl: false, alt: false }), null);
});

test('CMDK-21: JEV is never asked while the held library index is not current (Codex r4275482371)', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const effect = bar.slice(bar.indexOf('// One JEV call per pause in typing'), bar.indexOf('function setMode'));
	assert.match(effect, /if \(mode !== 'ai' \|\| !aiKeyKnown \|\| queryChars\(aiWords\) < AI_MIN_CHARS \|\| collectionPending \|\| collectionError !== null\) return;/);
	// The effect reads collectionPending, so it runs again (one call) once the current index lands.
	const guard = effect.indexOf('collectionPending');
	assert.ok(guard > 0 && guard < effect.indexOf('setTimeout('), 'checked before the timer is set');
	// Meanwhile the bar says what it waits for, not "Asking JEV".
	const pending = bar.indexOf('{:else if collectionPending}');
	assert.ok(pending > 0 && pending < bar.indexOf('Asking JEV about'));
	assert.match(bar, /\{:else if collectionPending\}\s*<li class="hint" role="presentation" data-testid="command-bar-ai-status">Reading the current library before asking JEV\.\.\.<\/li>/);
	// A refresh that failed leaves the old rows held but not current: JEV is not asked about
	// them, and the hint says why (Codex r4276184275).
	assert.match(bar, /\{:else if collectionError !== null\}\s*<li class="hint err" role="presentation" data-testid="command-bar-ai-status">AI search waits for the current library, which did not load: \{collectionError\}<\/li>\s*\{:else if collectionPending\}/);
	// Both come before the empty-shortlist line: an index that is not current yet (the first
	// load included) reads as waiting, never as "nothing mixes" (Codex r4276610760).
	const empty = bar.indexOf('{:else if aiShortlist.length === 0}');
	assert.ok(bar.indexOf('{:else if collectionError !== null}') < pending && pending < empty);
	// A ranked answer is shown only over a current index: stale or removed rows are never pickable (Codex r4276610750).
	const ranked = bar.slice(bar.indexOf('const aiRanked = $derived('), bar.indexOf('const aiFit'));
	assert.match(ranked, /aiState\.words === aiWords &&\s*!collectionPending &&\s*collectionError === null &&/);
	// ...and only for the track fields it scored: a refresh that edits a track under the same id
	// and query hides the old scores until the new call answers (Codex r4277881958).
	assert.match(ranked, /&&\s*cmdkAiAnswers\(aiWords, aiShortlist\.map\(aiTrack\)\)\s*\?/);
	const data = readFileSync(`${SRC}lib/rb/cmdk-ai-data.svelte.ts`, 'utf8');
	assert.match(data, /export function cmdkAiAnswers\(words: string, tracks: CmdkAiTrack\[\]\): boolean \{\s*return _asked === JSON\.stringify\(\[words, tracks\]\);/);
	// The same key the request is deduplicated on, so a ready answer and its question never drift apart.
	assert.match(data, /const asked = JSON\.stringify\(\[words, tracks\]\);/);
});

test('CMDK-21: JEV is asked once per pause in typing, never per keystroke or below the minimum words', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const effect = bar.slice(bar.indexOf('// One JEV call per pause in typing'), bar.indexOf('function setMode'));
	assert.match(effect, /if \(mode !== 'ai' \|\| !aiKeyKnown \|\| queryChars\(aiWords\) < AI_MIN_CHARS \|\| collectionPending \|\| collectionError !== null\) return;/);
	assert.match(effect, /setTimeout\([\s\S]*AI_DEBOUNCE_MS\)/);
	assert.match(effect, /return \(\) => clearTimeout\(timer\)/);
	assert.match(effect, /if \(untrack\(\(\) => cmdkAiKeyKnown\(\)\)\) requestCmdkAi/);
	assert.ok(ai.AI_DEBOUNCE_MS >= 300 && ai.AI_MIN_CHARS >= 2);
	// The key's answer is a dependency of the effect, so a key that resolves after the first pass still schedules the ask
	// (Codex P2, #6692). Only the flag is read, so a status change within the key's answer costs no second call.
	assert.match(bar, /const aiKeyKnown = \$derived\(cmdkAiKeyKnown\(\)\);/);
	// Only a confirmed key sets the flag: the initial and reset `idle` state does not (Codex P2, #6692).
	const data = readFileSync(`${SRC}lib/rb/cmdk-ai-data.svelte.ts`, 'utf8');
	assert.match(data, /if \(status\.configured\) \{\s*_keyKnown = true;/);
	assert.match(data, /export function resetCmdkAi\(\): void \{\s*_request\+\+;\s*_keyKnown = false;/);
	// A ranking shows only for the words in the box now.
	assert.match(bar, /aiState\.status === 'ready' &&\s*aiState\.words === aiWords &&/);
});

test('CMDK-21: a newer request wins and no key is its own state with one red toast', () => {
	const data = readFileSync(`${SRC}lib/rb/cmdk-ai-data.svelte.ts`, 'utf8');
	assert.equal((data.match(/if \(request !== _request\) return;/g) ?? []).length, 2, 'both answers check they are still the latest');
	assert.match(data, /if \(_state\.status !== 'no-key'\) toastCmdkAi\(NO_KEY\)/);
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	assert.match(bar, /aiState\.status === 'no-key'[\s\S]{0,400}data-testid="command-bar-ai-key"/);
	assert.match(bar, /title="Set OPENROUTER_API_KEY in the engine's environment/);
	// The page wires the toast through the bar, so neither the bar nor "/" first paint imports the toast store.
	assert.match(bar, /\$effect\(\(\) => setCmdkAiToast\(aiToast\)\);/);
	const page = readFileSync(`${SRC}routes/performance/+page.svelte`, 'utf8');
	assert.match(page, /aiToast=\{\(message\) => pushToast\(message, 'error'\)\}/);
	const init = readFileSync(`${SRC}lib/rb/app-init.ts`, 'utf8');
	assert.doesNotMatch(init, /cmdk-ai-toast/);
	assert.doesNotMatch(bar, /stores\.svelte/);
	assert.doesNotMatch(data, /stores\.svelte/);
});

test('CMDK-21: the sweep runs only while JEV answers and holds still under reduced motion', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	assert.match(bar, /\{#if mode === 'ai' && aiState\.status === 'working'\}<span class="sweep"/);
	assert.match(bar, /@media \(prefers-reduced-motion: reduce\) \{\s*\.sweep \{\s*animation: none;/);
});

test('CMDK-22: cmdk_mode reaches the mounted bar and takes only plain or ai', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		await assert.rejects(ipc.dispatchPerformanceCommand({ type: 'cmdk_mode', mode: 'ai' }), /requires the Cmd K v2 bar/);
		const modes = [];
		const unregister = ipc.registerCmdkAdapter({
			open: async () => {},
			close: () => {},
			snapshot: () => ({ open: true, query: '', selected: 0, rows: [], mode: 'plain', ai: 'idle' }),
			pick: async () => {},
			setMode: (mode) => {
				modes.push(mode);
				if (mode === 'ai' && modes.length > 2) throw new Error('AI search is off: turn on flag cmdk.v2.ai');
			}
		});
		await ipc.dispatchPerformanceCommand({ type: 'cmdk_mode', mode: 'ai' });
		await ipc.dispatchPerformanceCommand({ type: 'cmdk_mode', mode: 'plain' });
		await assert.rejects(ipc.dispatchPerformanceCommand({ type: 'cmdk_mode', mode: 'ai' }), /flag cmdk\.v2\.ai/);
		for (const mode of ['AI', 'search', undefined, 1]) {
			await assert.rejects(ipc.dispatchPerformanceCommand({ type: 'cmdk_mode', mode }), /plain' or 'ai'|unexpected|missing/);
		}
		await assert.rejects(ipc.dispatchPerformanceCommand({ type: 'cmdk_mode', mode: 'ai', extra: 1 }), /unexpected fields/);
		assert.deepEqual(modes, ['ai', 'plain', 'ai']);
		unregister();
	} finally {
		uninstall();
	}
});

test('CMDK-22: the bar refuses cmdk_mode while closed or with the flag off, and reports mode and fit', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const body = bar.slice(bar.indexOf('setMode: (next) =>'));
	assert.match(body, /if \(!open\) throw/);
	// Refused with the flag off whatever the mode asked, plain too: the bar shows no mode then (Codex r4276610753).
	assert.match(body, /if \(!aiOn\) throw new Error\('AI search is off: turn on flag cmdk\.v2\.ai'\);/);
	assert.doesNotMatch(body.slice(0, body.indexOf('if (next !== mode)')), /next === 'ai'/);
	assert.match(bar, /\.\.\.fitOf\(entry\.row\.stable_id\)/);
	assert.match(bar, /mode,\s*ai: aiShown/);
	// Below the threshold nothing is pending, so a stale ready answer reads idle, not working (Codex P2 BLOCKING, #6692).
	assert.match(bar, /queryChars\(aiWords\) < AI_MIN_CHARS\s*\?\s*'idle'\s*:\s*'working'/);
});

test('CMDK-22: AI mode shows error for a failed index and working while a ready answer waits on a pending one (Codex NON-BLOCKING, #6692)', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const shown = bar.slice(bar.indexOf('const aiShown = $derived('), bar.indexOf('const aiRanked = $derived('));
	assert.match(shown, /mode === 'ai' && collectionError !== null && aiState\.status !== 'no-key'\s*\?\s*'error'/);
	assert.match(shown, /mode === 'ai' && collectionPending && aiState\.status === 'ready'\s*\?\s*'working'/);
	// A failed answer for other words is stale too: it reads idle or working for the current query, never the old error (Codex P2 NON-BLOCKING, #6692).
	assert.match(shown, /\(aiState\.status === 'error' && aiState\.words !== aiWords\)/);
});

test('CMDK-20: the shortlist mixes into the master as it sounds: tempo fader and key shift applied', () => {
	const master = ai.audibleMaster({ bpm: 120, pitch: 1.05, key: '8A', key_shift_semitones: 2 });
	assert.ok(Math.abs(master.bpm - 126) < 1e-9);
	assert.equal(master.key, '10A');
	// Control: an untouched deck reads as its tags.
	assert.deepEqual(ai.audibleMaster({ bpm: 124, pitch: 1, key: '8A', key_shift_semitones: 0 }), { bpm: 124, key: '8A' });
	assert.deepEqual(ai.audibleMaster({ bpm: null, pitch: 1.1, key: null, key_shift_semitones: 0 }), { bpm: null, key: null });
	const rows = [ROW('at-tag', 120, '8A'), ROW('at-sound', 132, '10A')];
	assert.deepEqual(
		ai.shortlist(rows, master, new Set()).map((row) => row.stable_id),
		['at-sound']
	);
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const derived = bar.slice(bar.indexOf('const aiShortlist = $derived('), bar.indexOf('const aiRanked'));
	assert.match(derived, /masterDeck === null \? null : audibleMaster\(decks\[masterDeck\]\)/);
});

test('CMDK-21: the same words over the same tracks are not asked again while answered or in flight', () => {
	const data = readFileSync(`${SRC}lib/rb/cmdk-ai-data.svelte.ts`, 'utf8');
	const body = data.slice(data.indexOf('export function requestCmdkAi('), data.indexOf('const request = ++_request;', data.indexOf('export function requestCmdkAi(')));
	assert.match(body, /if \(asked === _asked && \(_state\.status === 'working' \|\| _state\.status === 'ready'\) && _state\.words === words\) return;/);
	assert.match(body, /_asked = asked;/);
	// Keyed on every submitted field, so edited metadata under the same ids asks again (Codex r4276714952).
	assert.match(body, /const asked = JSON\.stringify\(\[words, tracks\]\);/);
	assert.doesNotMatch(body, /track\.stable_id\)\.join/);
	// An error or a reset (idle) asks again, so a failed call is never stuck.
	assert.match(data, /\| \{ status: 'error'; words: string; message: string \}/);
});

test('CMDK-14: a guarded eject over IPC names the deck, not the guard object', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	assert.match(bar, /`deck \$\{outcome\.asked\} is playing; pick again with confirm to eject it`/);
	assert.doesNotMatch(bar, /`deck \$\{ejectGuard\} is playing/);
});

test('CMDK-21: the library status shows before a JEV failure left from before the index went stale (Codex r4277780851)', () => {
	const bar = readFileSync(new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url), 'utf8');
	const failed = bar.indexOf("{:else if aiState.status === 'error'}");
	const missing = bar.indexOf('{:else if collectionError !== null}\n\t\t\t\t\t\t\t<li class="hint err" role="presentation" data-testid="command-bar-ai-status">');
	const reading = bar.indexOf('Reading the current library before asking JEV...');
	assert.ok(missing > 0 && reading > missing && failed > reading, 'index failure, then index reading, then the old JEV failure');
	assert.ok(bar.indexOf("{#if aiState.status === 'no-key'}") < missing, 'a missing key still shows first');
});

test('CMDK-20: the AI shortlist excludes every track loaded this session, not only the recent few (Codex r4280339705)', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	assert.match(bar, /let playedIds = \$state<string\[\]>\(\[\]\);/);
	assert.match(bar, /new Set\(\[\.\.\.playedIds, \.\.\.DECK_IDS/);
	assert.match(bar, /if \(!untrack\(\(\) => playedIds\)\.includes\(stable_id\)\) playedIds = \[\.\.\.untrack\(\(\) => playedIds\), stable_id\];/);
});
