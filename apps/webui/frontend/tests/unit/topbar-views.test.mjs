/**
 * TOPVIEW-01..07: the top bar's view icons, PAD toggle, Track information
 * button and two-track AutoPlay, which were inert placeholders until Fri 9 Oct
 * 2026 (alpha status items I3-I7, I9).
 *
 * Regression lines:
 * - if a view icon is still `disabled`/`rb-inert` then clicking it does nothing
 * - if the four view icons do not share one pref then two can be lit at once
 * - if List view leaves the waveform track its height then the library gains no rows
 * - if the phase math answers 0 where it cannot measure then "no grid" reads
 *   as "perfectly in phase"
 * - if the phase offset is not wrapped then a deck a whole beat late reads as
 *   a beat out instead of in phase
 * - if Track information opens an empty deck then the overlay shows nothing real
 * - if two-track AutoPlay reaches deck 3 or 4 then it took the DJ's own deck
 * - if two-track AutoPlay is off and the follower choice changed then the
 *   PLAY-22 default regressed (mutation in the opposite direction)
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, describe, it, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';
import { storedGrid, trueBeatTimes } from './phase-lock-workload.mjs';

const read = (rel) => readFileSync(fileURLToPath(new URL(`../../src/${rel}`, import.meta.url)), 'utf8');
const TOPBAR = read('lib/components/rb/TopBar.svelte');
const PAGE = read('routes/performance/+page.svelte');
const AREA = read('lib/components/rb/PerfViewArea.svelte');

/** A 120 BPM grid (0.5 s beats) whose first beat is beat `firstN` of its bar. */
function grid(count, { start = 0, period = 0.5, firstN = 1 } = {}) {
	return Array.from({ length: count }, (_, i) => ({
		t: start + i * period,
		n: ((firstN - 1 + i) % 4) + 1,
		bpm: 60 / period
	}));
}

let math;
let lock;
let focus;
let info;
let prefs;
let autoPlay;
let gridProv;
let apiErrors;
let infoState;
before(async () => {
	math = await loadTypeScriptModule('src/lib/rb/phase-view-math.ts');
	lock = await loadTypeScriptModule('src/lib/rb/phase-lock.ts');
	focus = await loadTypeScriptModule('src/lib/rb/focused-deck.ts');
	info = await loadTypeScriptModule('src/lib/rb/track-info.ts');
	prefs = await loadTypeScriptModule('src/lib/rb/topbar-view-prefs.ts');
	autoPlay = await loadTypeScriptModule('src/lib/rb/auto-play.ts');
	gridProv = await loadTypeScriptModule('src/lib/rb/grid-provenance.ts');
	apiErrors = await loadTypeScriptModule('src/lib/api/errors.ts');
	infoState = await loadTypeScriptModule('tests/unit/fixtures/track-info-focus-entry.ts');
});

describe('TOPVIEW-01..04: the view icons and PAD are live controls', () => {
	it('every view icon dispatches set_perf_view for the one perf_view pref and is lit only for its own view', () => {
		for (const view of ['list', 'waveform', 'phase-scope', 'phase-meter']) {
			const re = new RegExp(
				`class:active=\\{uiPrefs\\.perf_view === '${view}'\\}[\\s\\S]*?aria-pressed=\\{uiPrefs\\.perf_view === '${view}'\\}` +
					`[\\s\\S]*?onclick=\\{\\(\\) => void runPerformanceCommandFromUi\\(\\{ type: 'set_perf_view', view: '${view}' \\}\\)\\}`
			);
			assert.match(TOPBAR, re, `${view} icon is not wired to the set_perf_view dispatcher command`);
		}
		for (const view of ['list', 'waveform', 'phase-scope', 'phase-meter']) {
			const at = TOPBAR.indexOf(`data-testid="perf-view-${view}"`);
			assert.ok(at !== -1, `${view} icon has no test id`);
			const button = TOPBAR.slice(TOPBAR.lastIndexOf('<button', at), TOPBAR.indexOf('</button>', at));
			assert.doesNotMatch(button, /rb-inert/, `${view} icon is still inert`);
			assert.doesNotMatch(button, /\bdisabled\b/, `${view} icon is still disabled`);
		}
	});

	it('PAD toggles show_pads and the route hides every pad bank when it is off', () => {
		assert.match(
			TOPBAR,
			/data-testid="topbar-pad-toggle"[\s\S]*?onclick=\{\(\) => void runPerformanceCommandFromUi\(\{ type: 'show_pads', enabled: !uiPrefs\.show_pads \}\)\}/
		);
		assert.match(PAGE, /class:pads-hidden=\{!uiPrefs\.show_pads\}/);
		assert.match(PAGE, /\.perf-root\.pads-hidden :global\(\.cue-area > \.bank\) \{\s*display: none;/);
	});

	it('List view collapses only the wavestack track; the deck track keeps the real row height', () => {
		assert.match(PAGE, /class:perf-view-list=\{perfViewCollapsesWavestack\(uiPrefs\.perf_view\)\}/);
		const rule = PAGE.match(/\.perf-root\.perf-view-list \{([^}]*)\}/);
		assert.ok(rule, 'no List view rule');
		assert.match(rule[1], /--rb-wavestack-shown: 0;/);
		// if list view zeroes the row variables, the deck track (built from them) grows in short windows - broken.
		assert.doesNotMatch(rule[1], /--rb-waverow-h|--rb-stemwave-stack-extra/);
		for (const n of ['4', '2']) {
			assert.match(PAGE, new RegExp(`calc\\(${n} \\* \\(var\\(--rb-waverow-h\\) \\+ var\\(--rb-stemwave-stack-extra, 0px\\)\\) \\* var\\(--rb-wavestack-shown, 1\\)\\)`));
		}
		assert.equal((PAGE.match(/--rb-wavestack-shown/g) ?? []).length, 4, 'two tracks, the rule and its comment');
	});

	it('the route mounts only the chosen view in the wavestack area', () => {
		assert.match(PAGE, /<PerfViewArea view=\{uiPrefs\.perf_view\} lessLayout=\{uiPrefs\.deck_layout === 'less'\} \/>/);
		assert.doesNotMatch(PAGE, /<WaveformStack \/>/);
		assert.match(AREA, /\{#if mount === 'waveform'\}\s*<WaveformStack \/>/);
		assert.match(AREA, /mount === 'phase-scope'\}\s*\{#await loadPhaseScope\(\) then \{ default: PhaseScopeView \}\}\s*<PhaseScopeView \{lessLayout\} \/>/);
		assert.match(AREA, /mount === 'phase-meter'\}\s*\{#await loadPhaseMeter\(\) then \{ default: PhaseMeterView \}\}\s*<PhaseMeterView \{lessLayout\} \/>/);
		assert.match(AREA, /perfViewWavestackMount/);
		// The phase views stay out of the first-paint performance chunk.
		assert.match(AREA, /import\('\.\/wave\/PhaseScopeView\.svelte'\)/);
		assert.match(AREA, /import\('\.\/wave\/PhaseMeterView\.svelte'\)/);
		assert.doesNotMatch(AREA, /import Phase(Scope|Meter)View from/);
	});

	it('a phase view that fails to load shows its error in the phase-view region (Sol P1 4349621975)', () => {
		// Both views' rejections render the one snippet, so the region hooks on it cover both.
		assert.equal((AREA.match(/\{:catch err\}\s*\{@render failed\(err\)\}/g) ?? []).length, 2);
		// In the wavestack slot, not wherever the grid puts an unplaced child...
		assert.match(AREA, /<p class="view-load-error rb-phase-view" role="alert">/);
		assert.match(AREA, /\.view-load-error \{\s*grid-area: wavestack;/);
		// ...and hidden with the top region in technically-working mode, like the views.
		assert.match(PAGE, /\.perf-root\.tw-active :global\(\.rb-phase-view\) \{\s*opacity: 0;\s*pointer-events: none;/);
		assert.match(PAGE, /\.perf-root\.tw-active\.tw-top-visible :global\(\.rb-phase-view\) \{\s*opacity: 1;/);
	});
});

describe('top-bar view prefs validation', () => {
	it('accepts the four views and refuses anything else', () => {
		for (const v of ['waveform', 'list', 'phase-scope', 'phase-meter']) assert.equal(prefs.isPerfView(v), true);
		assert.equal(prefs.isPerfView('grid'), false);
		assert.throws(() => prefs.assertPerfView('grid'), /perf_view must be one of/);
		assert.throws(() => prefs.validateTopbarViewFields({ perf_view: 'grid' }, 'k'), /k: malformed prefs blob/);
		assert.throws(() => prefs.validateTopbarViewFields({ show_pads: 'yes' }, 'k'), /show_pads is not a boolean/);
	});

	it('setters persist and sync each change to disk prefs', () => {
		const state = { ...prefs.TOPBAR_VIEW_DEFAULTS };
		let persisted = 0;
		const synced = [];
		const s = prefs.makeTopbarViewSetters(state, () => (persisted += 1), (p) => synced.push(p));
		s.setPerfView('phase-meter');
		s.setShowPads(false);
		s.setAutoPlayTwoDeck(true);
		assert.deepEqual(state, { perf_view: 'phase-meter', show_pads: false, auto_play_two_deck: true });
		assert.equal(persisted, 3);
		assert.deepEqual(synced, [{ perf_view: 'phase-meter' }, { show_pads: false }, { auto_play_two_deck: true }]);
		assert.throws(() => s.setPerfView('grid'));
		assert.equal(state.perf_view, 'phase-meter', 'a refused view must not land');
	});

	it('disk hydrate copies a valid body and leaves absent fields alone', () => {
		const state = { ...prefs.TOPBAR_VIEW_DEFAULTS };
		prefs.hydrateTopbarViewFromDisk(state, { perf_view: 'list', auto_play_two_deck: true });
		assert.deepEqual(state, { perf_view: 'list', show_pads: true, auto_play_two_deck: true });
	});

	it('disk hydrate refuses a malformed body loudly and writes none of it', () => {
		for (const [body, why] of [
			[{ perf_view: 'grid', auto_play_two_deck: true }, /perf_view must be/],
			[{ show_pads: 'no', perf_view: 'list' }, /show_pads is not a boolean/],
			[{ auto_play_two_deck: 1, perf_view: 'list' }, /auto_play_two_deck is not a boolean/]
		]) {
			const state = { ...prefs.TOPBAR_VIEW_DEFAULTS };
			assert.throws(() => prefs.hydrateTopbarViewFromDisk(state, body), why);
			assert.deepEqual(state, prefs.TOPBAR_VIEW_DEFAULTS, `a refused body must not half-apply: ${JSON.stringify(body)}`);
		}
	});

	it('the ui-prefs hydrator fails on a refused top-bar body instead of swallowing it', () => {
		const HYDRATE = read('lib/rb/prefs-hydrate.ts');
		assert.match(
			HYDRATE,
			/try \{\s*hydrateTopbarViewFromDisk\(uiPrefs, body\);\s*\} catch \(exc\) \{\s*contractError \?\?= new Error\(\s*`Top-bar view prefs on disk were refused/
		);
	});
});

describe('TOPVIEW-01: a failed ui-prefs GET is reported, never swallowed (Sol P1 4286445859)', () => {
	it('a failed ui-prefs GET rejects the hydration, as main\'s boot path reports it (review P1, PR #7483)', () => {
		const HYDRATE = read('lib/rb/prefs-hydrate.ts');
		assert.match(HYDRATE, /const body = \(await unwrap\(api\.GET\('\/api\/v1\/ui-prefs'\)\)\) as DiskPrefsPatch;/);
		const start = HYDRATE.indexOf('export function makePrefsHydrator(');
		assert.ok(start >= 0, 'the hydrator is in this file');
		const hydrator = HYDRATE.slice(start);
		assert.doesNotMatch(hydrator, /\} catch \{|\/\* ignore \*\//, 'no ui-prefs GET failure is swallowed');
	});

	it('the wording helper reads the no-answer texts, and only TypeErrors', () => {
		for (const text of ['Failed to fetch', 'Load failed', 'NetworkError when attempting to fetch resource.', 'fetch failed']) {
			assert.equal(apiErrors.readsAsUnreachableFetch(new TypeError(text)), true, text);
		}
		assert.equal(apiErrors.readsAsUnreachableFetch(new Error('Failed to fetch')), false);
		assert.equal(apiErrors.readsAsUnreachableFetch(new TypeError('x is not a function')), false);
	});

	it('a malformed top-bar view pref keeps the local values and fails the hydration after the rest applies', () => {
		const HYDRATE = read('lib/rb/prefs-hydrate.ts');
		assert.match(
			HYDRATE,
			/try \{\s*hydrateTopbarViewFromDisk\(uiPrefs, body\);\s*\} catch \(exc\) \{\s*contractError \?\?= new Error\(/
		);
		assert.match(HYDRATE, /persist\(\);\s*if \(contractError !== null\) throw contractError;/);
	});
});

describe('TOPVIEW-02/03: phase math', () => {
	it('bar phase follows each beat\'s own number, not the beat count since the start', () => {
		const beats = grid(16, { firstN: 3 }); // starts on beat 3 of its bar
		assert.deepEqual(math.barPhaseAt(beats, 0), { phase: 0.5, beatInBar: 3 });
		assert.deepEqual(math.barPhaseAt(beats, 1.25), { phase: 0.125, beatInBar: 1 });
	});

	it('the Phase scope reads a folded follower against the master so locked decks turn together (Sol P1 4302401669, 4302494490)', () => {
		const master = grid(64); // 120 BPM, a beat every 0.5 s
		const slow = grid(32, { period: 1 }); // 60 BPM follower locked at 0.5x
		const fast = grid(128, { period: 0.25 }); // 240 BPM follower locked at 2x
		// The join may pair any follower beat with the master's: these grids are the
		// same followers numbered from beat 2 and beat 3, which is what a folded BAR
		// join that landed off the follower's downbeat looks like.
		const slowOffBar = grid(32, { period: 1, firstN: 2 });
		const fastOffBar = grid(128, { period: 0.25, firstN: 3 });
		for (const sec of [4.0, 4.6, 5.3, 7.9]) {
			const at = { beats: master, positionSec: sec };
			const m = math.scopeBarPhase(master, sec, null, null);
			for (const [name, beats, fold] of [
				['0.5x', slow, 0.5],
				['2x', fast, 2],
				['0.5x joined on beat 2', slowOffBar, 0.5],
				['2x joined on beat 3', fastOffBar, 2]
			]) {
				const f = math.scopeBarPhase(beats, sec, fold, at);
				assert.ok(Math.abs(f.phase - m.phase) < 1e-9, `${name} follower at ${sec}s: ${f.phase} vs ${m.phase}`);
				assert.equal(f.beatInBar, m.beatInBar, `${name} follower at ${sec}s`);
			}
		}
		// Real drift still shows, in master beats: 0.1 of a beat early is 0.025 of a bar ahead.
		const early = math.scopeBarPhase(slow, 4.05, 0.5, { beats: master, positionSec: 4.0 });
		assert.ok(Math.abs(early.phase - (math.barPhaseAt(master, 4.0).phase + 0.025)) < 1e-9, `drift: ${early.phase}`);
		// Wraps at the bar: a follower just behind the master's downbeat reads at the end of the bar.
		const late = math.scopeBarPhase(slow, 3.95, 0.5, { beats: master, positionSec: 4.0 });
		assert.ok(Math.abs(late.phase - 0.975) < 1e-9, `wrap: ${late.phase}`);
		assert.equal(late.beatInBar, 4);
		// Control: unlocked, the same followers turn at their own rate, which is the drift the fold removes.
		assert.notEqual(math.scopeBarPhase(slow, 4.6, null, null).phase, math.scopeBarPhase(master, 4.6, null, null).phase);
		// With no lock it is exactly barPhaseAt, whatever the master, so an unlocked deck keeps its own bar numbers.
		const offset = grid(16, { firstN: 3 });
		assert.deepEqual(math.scopeBarPhase(offset, 1.25, null, { beats: master, positionSec: 9 }), math.barPhaseAt(offset, 1.25));
		// A grid with a short bar (1 2 3 4 1 2 3 1 2 ...) restarts its count; unlocked, the
		// scope follows those numbers rather than counting beats since the first downbeat.
		const shortBar = [1, 2, 3, 4, 1, 2, 3, 1, 2, 3, 4].map((n, i) => ({ t: i * 0.5, n, bpm: 120 }));
		assert.deepEqual(math.scopeBarPhase(shortBar, 4.25, null, null), { phase: 0.375, beatInBar: 2 });
		// A folded deck it cannot measure against says so: no master, either deck off its grid.
		assert.equal(math.scopeBarPhase(slow, 4.0, 0.5, null), null);
		assert.equal(math.scopeBarPhase(slow, 99, 0.5, { beats: master, positionSec: 4.0 }), null);
		assert.equal(math.scopeBarPhase(slow, 4.0, 0.5, { beats: master, positionSec: 99 }), null);
		const VIEW = read('lib/components/rb/wave/PhaseScopeView.svelte');
		assert.match(VIEW, /const lock = phaseLockFold\(rustMode\.enabled, phaseLocks\[id\]\?\.normalization, \(\) => beatSyncFold\(id\)\);/);
		assert.match(VIEW, /scopeBarPhase\(beats, playheadMs\(id, deck\) \/ 1000, lock, scopeMaster\(id\)\)/);
		assert.match(VIEW, /if \(masterId === null \|\| masterId === id\) return null;/);
		assert.doesNotMatch(VIEW, /barPhaseAt\(/, 'the scope never reads a deck without its fold');
	});

	it('the Phase scope reads an exact-tempo Beat Sync lock against the master, whichever beats a BEAT join paired (Sol P1 4349021851)', () => {
		const master = grid(64); // 120 BPM, numbered from its downbeat
		// The same tempo, numbered so the BEAT join put master beat 2 on follower beat 4:
		// follower beat n sits where master beat ((n - 3) mod 4) + 1 does.
		const joined = grid(64, { firstN: 3 });
		for (const sec of [4.0, 4.6, 5.3, 7.9]) {
			const m = math.scopeBarPhase(master, sec, null, null);
			const f = math.scopeBarPhase(joined, sec, 1, { beats: master, positionSec: sec });
			assert.ok(Math.abs(f.phase - m.phase) < 1e-9, `locked at 1x, ${sec}s: ${f.phase} vs ${m.phase}`);
			assert.equal(f.beatInBar, m.beatInBar, `locked at 1x, ${sec}s`);
			// Control, opposite direction: the same deck with no lock keeps its own bar,
			// half a bar from the master's, so a lock is what moves it.
			const own = math.scopeBarPhase(joined, sec, null, { beats: master, positionSec: sec });
			assert.deepEqual(own, math.barPhaseAt(joined, sec));
			assert.ok(Math.abs(own.phase - m.phase - 0.5) < 1e-9 || Math.abs(own.phase - m.phase + 0.5) < 1e-9, `unlocked, ${sec}s`);
		}
		// Drift on an exact-tempo lock still shows: 0.1 of a beat early is 0.025 of a bar ahead.
		const early = math.scopeBarPhase(joined, 4.05, 1, { beats: master, positionSec: 4.0 });
		assert.ok(Math.abs(early.phase - (math.barPhaseAt(master, 4.0).phase + 0.025)) < 1e-9, `drift: ${early.phase}`);
		// A locked deck with no master to read against says so.
		assert.equal(math.scopeBarPhase(joined, 4.0, 1, null), null);
	});

	it('cannot measure off the grid and says so with null, never 0', () => {
		const beats = grid(8, { start: 2 });
		assert.equal(math.barPhaseAt(beats, 1), null);
		assert.equal(math.barPhaseAt(beats, 99), null);
		assert.equal(math.barPhaseAt([], 1), null);
		assert.equal(math.phaseOffsetBeats(beats, 1, beats, 3, 1), null);
	});

	it('measures the follower ahead (+) or behind (-) the master, wrapped to half a beat', () => {
		const beats = grid(64);
		assert.equal(math.phaseOffsetBeats(beats, 4.0, beats, 4.0, 1), 0);
		assert.ok(Math.abs(math.phaseOffsetBeats(beats, 4.0, beats, 4.05, 1) - 0.1) < 1e-9);
		assert.ok(Math.abs(math.phaseOffsetBeats(beats, 4.05, beats, 4.0, 1) + 0.1) < 1e-9);
		// A whole beat late is in phase at beat level.
		assert.equal(math.phaseOffsetBeats(beats, 4.5, beats, 4.0, 1), 0);
		// 0.4 of a beat behind reads as behind, not 0.6 ahead.
		assert.ok(Math.abs(math.phaseOffsetBeats(beats, 4.2, beats, 4.0, 1) + 0.4) < 1e-9);
	});

	it('a follower synced through a 0.5x or 2x tempo fold reads in phase, not drifting (Sol P1 4301475291)', () => {
		const master = grid(64); // 120 BPM
		// Beat Sync folds these instead of pitching them 2x: each plays at 1x, so
		// the 60 BPM grid passes half a beat per master beat (normalization 0.5)
		// and the 240 BPM grid two (normalization 2), both in phase.
		const slow = grid(64, { period: 1 });
		const fast = grid(128, { period: 0.25 });
		for (const t of [4.0, 5.25, 7.5]) {
			assert.equal(math.phaseOffsetBeats(master, t, slow, t, 0.5), 0, `half-time lock at ${t}`);
			assert.equal(math.phaseOffsetBeats(master, t, fast, t, 2), 0, `double-time lock at ${t}`);
		}
		// Without the fold the locked half-time follower drifts and reads out of
		// phase on most beats: the defect this fold removes.
		assert.notEqual(math.phaseOffsetBeats(master, 5.25, slow, 5.25, 1), 0);
		// A double-time follower meets a master beat on every one of its own
		// beats, so one landing between master beats is in phase too.
		assert.equal(math.phaseOffsetBeats(master, 4.0, fast, 4.25, 2), 0);
		// A real offset still shows through the fold, in master beats.
		assert.ok(Math.abs(math.phaseOffsetBeats(master, 4.0, slow, 4.05, 0.5) - 0.1) < 1e-9);
		assert.ok(Math.abs(math.phaseOffsetBeats(master, 4.0, fast, 4.05, 2) - 0.1) < 1e-9);
	});

	it('a pair the lock holds on a flagged dynamic grid reads in phase, as the lock reads it (Sol P1 4350124721)', () => {
		const master = grid(520, { start: 0.2 });
		// A rekordbox dynamic grid: 10 ms storage steps, 6 ms jitter, one beat in
		// twenty 30 ms off. The lock smooths it; the raw grid would not.
		const follower = storedGrid(trueBeatTimes({ beats: 520, bpmAt: () => 120 }), {
			stepMs: 10,
			jitterMs: 6,
			strayShare: 0.05,
			strayMs: 30,
			seed: 7
		});
		const held = lock.smoothedBeatTimes(follower);
		let rawOff = 0;
		for (let k = 40; k < 480; k += 7) {
			const masterSec = master[k].t + 0.1;
			const followerSec = held[k] + 0.1 * ((held[k + 1] - held[k]) / 0.5);
			const lockMs = lock.phaseErrorMs({
				masterBeats: master,
				masterPositionSec: masterSec,
				masterTempo: 1,
				followerBeats: follower,
				followerPositionSec: followerSec,
				followerBaseTempo: 1,
				pitchRangePct: 8
			});
			assert.ok(Math.abs(lockMs) < 1e-6, `the lock holds beat ${k} (${lockMs} ms)`);
			const view = math.phaseOffsetBeats(master, masterSec, follower, followerSec, 1);
			assert.ok(Math.abs(view) < 1e-9, `the view reads beat ${k} in phase, got ${view}`);
			// Control: the raw stored grid reads the same instant out of phase.
			const raw = lock.gridBeatPosition(follower, followerSec) - lock.gridBeatPosition(master, masterSec);
			rawOff = Math.max(rawOff, Math.abs(raw - Math.round(raw)));
		}
		assert.ok(rawOff > 0.01, `the raw grid must disagree somewhere, or this pair is not flagged (${rawOff})`);
		// An even pair is untouched: the lock grid IS the stored grid there.
		const even = grid(64);
		assert.ok(Math.abs(math.phaseOffsetBeats(even, 4.0, even, 4.05, 1) - 0.1) < 1e-9);
	});

	it('the Phase meter says the master tempo is missing instead of switching to beats (Sol P1 4302183122)', () => {
		// Run the reading the view renders from (Codex P1 4302220951): no tempo,
		// a zero tempo and a non-finite one each read "no tempo" and keep the
		// beat offset for the needle; a real tempo still reads ms.
		for (const bpm of [null, 0, -120, Number.NaN]) {
			const reading = math.phaseMeterReading(0.1, bpm);
			assert.deepEqual(reading, { kind: 'no-tempo', beats: 0.1 }, `master tempo ${bpm}`);
			assert.equal(math.phaseMeterReadingText(reading), 'no tempo');
		}
		// Control, opposite direction: a usable tempo is never reported missing.
		const reading = math.phaseMeterReading(0.1, 120);
		assert.deepEqual(reading, { kind: 'offset', beats: 0.1, ms: 50 });
		assert.equal(math.phaseMeterReadingText(reading), '+50 ms');
		assert.equal(math.phaseMeterReadingText(math.phaseMeterReading(-0.03, 128)), '-14 ms');
		// The view renders exactly that reading and text, with no beat fallback of its own.
		const VIEW = read('lib/components/rb/wave/PhaseMeterView.svelte');
		assert.match(VIEW, /return phaseMeterReading\(beats, bpm\);/);
		assert.match(VIEW, /case 'no-tempo':\s*case 'offset':\s*return phaseMeterReadingText\(s\);/);
		assert.match(VIEW, /the master deck's tempo is unavailable, so the offset cannot be given in ms/);
		assert.doesNotMatch(VIEW, /phaseOffsetMs|formatOffsetMs|toFixed\(2\)\} beat`/, 'the view does no unit conversion of its own');
	});

	it('the Phase meter reads the fold the active engine locked the follower at, not its BPM ratio', async () => {
		const VIEW = read('lib/components/rb/wave/PhaseMeterView.svelte');
		assert.match(VIEW, /const fold = phaseMeterFold\(rustMode\.enabled, phaseLocks\[id\]\?\.normalization, \(\) => beatSyncFold\(id\)\);/);
		assert.doesNotMatch(VIEW, /tempoFoldBetween|deckEffectiveBpm\(id\)/, 'an unlocked deck at 2:1 tempo is not double time (Sol P1 4301847502)');
		assert.match(VIEW, /playheadMs\(id, deck\) \/ 1000,\s*fold\s*\);/);
		const ENGINE = read('lib/rb/audio-engine.svelte.ts');
		assert.match(ENGINE, /^export const beatSyncFold = _phaseLock\.normalizationOf;/m);
		// Sol P1 4301959423: Rust mode reads its own lock map, never the Web Audio one.
		const fold = await loadTypeScriptModule('src/lib/rb/phase-meter-fold.ts');
		const webAudio = () => 0.5;
		assert.equal(fold.phaseMeterFold(true, 2, webAudio), 2, 'a Rust follower locked at double time');
		assert.equal(fold.phaseMeterFold(true, undefined, webAudio), 1, 'a Rust deck with no lock has no fold');
		assert.equal(fold.phaseMeterFold(false, 2, webAudio), 0.5, 'control: Web Audio mode reads the Web Audio lock');
		assert.equal(fold.phaseMeterFold(false, 2, () => null), 1, 'a Web Audio deck with no lock has no fold');
		// The scope needs lock presence, not just the fold: an exact-tempo lock is 1, no lock is null (Sol P1 4349021851).
		assert.equal(fold.phaseLockFold(true, 1, webAudio), 1, 'a Rust follower locked at exact tempo');
		assert.equal(fold.phaseLockFold(true, undefined, webAudio), null, 'a Rust deck with no lock');
		assert.equal(fold.phaseLockFold(false, undefined, () => 1), 1, 'a Web Audio follower locked at exact tempo');
		assert.equal(fold.phaseLockFold(false, 1, () => null), null, 'a Web Audio deck with no lock, whatever Rust holds');
		// The phase math has no default fold to fall back on (Sol P1 4301959429).
		assert.match(read('lib/rb/phase-view-math.ts'), /\tnormalization: TempoNormalization\n\): number \| null \{/);
	});

	it('converts beats to ms at the master tempo and labels the sign', () => {
		assert.equal(math.phaseOffsetMs(0.1, 120), 50);
		assert.equal(math.phaseOffsetMs(0.1, 0), null);
		assert.equal(math.formatOffsetMs(12.4), '+12 ms');
		assert.equal(math.formatOffsetMs(-3.6), '-4 ms');
		assert.equal(math.formatOffsetMs(0.2), '0 ms');
	});
});

describe('TOPVIEW-01/04: narrow width tiers never strand a persisted choice', () => {
	const TOPBAR_SRC = readFileSync(new URL('../../src/lib/components/rb/TopBar.svelte', import.meta.url), 'utf8');
	it('view icons and PAD yield only at their defaults', () => {
		assert.match(TOPBAR_SRC, /class:view-chosen=\{uiPrefs\.perf_view !== 'waveform'\}/);
		assert.match(TOPBAR_SRC, /class:pads-off=\{!uiPrefs\.show_pads\}/);
		const viewRules = TOPBAR_SRC.match(/\.rb-topbar:not\(\.view-chosen\):not\(\.single-click\) \.icon-cluster > :global\(\.explainer:has\(\.tb-view\)\)/g) ?? [];
		const padRules = TOPBAR_SRC.match(/\.rb-topbar:not\(\.pads-off\) \.topbar-slot-pad \{ display: none; \}/g) ?? [];
		assert.equal(viewRules.length, 2, 'both narrow tiers must gate the view icons on the default view');
		assert.equal(padRules.length, 2, 'both narrow tiers must gate PAD on pads being shown');
		// Mutation guard, other direction: no rule hides them unconditionally.
		assert.doesNotMatch(TOPBAR_SRC, /:has\(\.rb-inert, \.tb-view\)/);
		assert.doesNotMatch(TOPBAR_SRC, /\t\.rb-topbar \.topbar-slot-pad \{ display: none; \}/);
	});
});

describe('TOPVIEW-05: focused deck and the track record', () => {
	it('Last played shows the later of the Open DJ and imported timestamps', () => {
		// Codex P2, PR #6228: `??` always preferred the Open DJ play, even when rekordbox's was newer.
		const row = (opendj, imported) =>
			info
				.trackInfoRows({ stable_id: 's', last_played_at: imported, provenance: {} }, {
					opendj_play_count: 1,
					rekordbox_play_count: 1,
					opendj_last_played_at: opendj
				})
				.find((r) => r.label === 'Last played').value;
		assert.equal(row('2026-10-01T00:00:00Z', '2026-10-20T00:00:00Z'), '2026-10-20T00:00:00Z');
		// Control, other direction: a newer Open DJ play still wins.
		assert.equal(row('2026-10-21T00:00:00Z', '2026-10-20T00:00:00Z'), '2026-10-21T00:00:00Z');
		assert.equal(row(null, '2026-10-20T00:00:00Z'), '2026-10-20T00:00:00Z');
		assert.equal(row('2026-10-21T00:00:00Z', null), '2026-10-21T00:00:00Z');
	});

	const decks = (loaded, master) =>
		[1, 2, 3, 4].map((id) => ({ id, loaded: loaded.includes(id), isMaster: id === master }));

	it('opens the one selected deck while it holds a track, else the master, else the first loaded', () => {
		assert.equal(focus.singleSelectedDeck([2]), 2);
		assert.equal(focus.singleSelectedDeck([1, 2]), null);
		assert.equal(focus.singleSelectedDeck([]), null);
		assert.equal(focus.resolveFocusedDeck(3, decks([1, 3], 1)), 3);
		assert.equal(focus.resolveFocusedDeck(4, decks([1, 2], 2)), 2);
		assert.equal(focus.resolveFocusedDeck(null, decks([2, 4], null)), 2);
		assert.equal(focus.resolveFocusedDeck(1, decks([], null)), null);
	});

	it('the open record stays on the deck and track it was opened for, and closes when either changes', () => {
		const ids = { 1: 'a', 2: 'b', 3: null, 4: null };
		const of = (deck) => ids[deck];
		const opened = { deckId: 2, stableId: 'b' };
		assert.deepEqual(focus.openedTrackInfo(opened, of), { deckId: 2, stableId: 'b' });
		ids[2] = null; // deck 2 unloads while deck 1 still holds a track
		assert.equal(focus.openedTrackInfo(opened, of), null, 'the record fell through to another deck');
		// Control: the focus rule itself still falls back to deck 1, which is why the
		// overlay must not follow it.
		assert.equal(focus.resolveFocusedDeck(2, decks([1], null)), 1);
		ids[2] = 'c'; // the same deck takes a new track: the record opened for 'b' closes
		assert.equal(focus.openedTrackInfo(opened, of), null, 'the record swapped to the deck\'s new track');
		ids[2] = 'b';
		assert.deepEqual(focus.openedTrackInfo(opened, of), opened, 'unchanged deck and track stay open');
		assert.equal(focus.openedTrackInfo(null, of), null);
		const button = read('lib/components/rb/TrackInfoButton.svelte');
		const host = read('lib/components/rb/TrackInfoHost.svelte');
		assert.match(host, /stableId: deckStates\[id\]\.stable_id/);
		assert.match(host, /if \(trackInfo\.opened !== null && shown === null\) closeTrackInfo\(\);/);
		assert.match(host, /deckId=\{shown\.deckId\}\s*stableId=\{shown\.stableId\}/);
		assert.doesNotMatch(host, /deckId=\{focused\}/, 'the overlay must not follow the focus rule');
		// Codex P1 4300879440: the click goes through the dispatcher, never a local toggle.
		assert.match(button, /onclick=\{ontoggle\}/);
		const topbar = read('lib/components/rb/TopBar.svelte');
		assert.match(topbar, /<TrackInfoButton\s*ontoggle=\{\(\) => void runPerformanceCommandFromUi\(\{ type: 'track_info_toggle' \}\)\}\s*\/>/);
		assert.match(topbar, /<TrackInfoHost onclose=\{\(\) => void runPerformanceCommandFromUi\(\{ type: 'track_info_close' \}\)\} \/>/);
		assert.doesNotMatch(button, /let open = \$state/);
		assert.doesNotMatch(host, /let open = \$state/);
	});

	it('the shared open state toggles for the focused deck, follows its track and refuses an empty table', () => {
		// track-info-state.svelte.ts is the one state the button and the
		// dispatcher's track_info_toggle / track_info_close both drive.
		const table = (ids, master = null) =>
			[1, 2, 3, 4].map((id) => ({
				id,
				loaded: ids[id] != null,
				isMaster: id === master,
				stableId: ids[id] ?? null
			}));
		const ids = { 1: 'a', 2: 'b' };
		infoState.closeTrackInfo();
		assert.deepEqual(infoState.trackInfoSnapshot(table(ids)), { open: false, deck: null, stable_id: null });
		infoState.toggleTrackInfo(table(ids, 2));
		// Before any click, deck 1 is only the default selection: the master wins (Sol P1 4350579790).
		assert.deepEqual(infoState.trackInfoSnapshot(table(ids, 2)), { open: true, deck: 2, stable_id: 'b' });
		infoState.closeTrackInfo();
		// A click on deck 1 makes it the focus, over master deck 2.
		infoState.clickSelect(1, false);
		infoState.toggleTrackInfo(table(ids, 2));
		assert.deepEqual(infoState.trackInfoSnapshot(table(ids, 2)), { open: true, deck: 1, stable_id: 'a' });
		infoState.closeTrackInfo();
		// A later load on deck 2 moves the selection Space follows, not the clicked focus (Sol P1 4351433888).
		infoState.onDeckLoadStart(2);
		assert.deepEqual(infoState.mixerSelection.selected, [2], 'control: the load did move the shared selection');
		infoState.toggleTrackInfo(table(ids, 2));
		assert.deepEqual(infoState.trackInfoSnapshot(table(ids, 2)), { open: true, deck: 1, stable_id: 'a' });
		// A click on deck 2 does move it.
		infoState.closeTrackInfo();
		infoState.clickSelect(2, false);
		infoState.toggleTrackInfo(table(ids, 2));
		assert.deepEqual(infoState.trackInfoSnapshot(table(ids, 2)), { open: true, deck: 2, stable_id: 'b' });
		infoState.closeTrackInfo();
		// A shift-click that leaves two decks selected is no single choice: with no master, the lowest loaded deck.
		infoState.clickSelect(1, false);
		infoState.clickSelect(2, true);
		infoState.toggleTrackInfo(table(ids));
		assert.deepEqual(infoState.trackInfoSnapshot(table(ids)), { open: true, deck: 1, stable_id: 'a' });
		infoState.closeTrackInfo();
		infoState.clickSelect(2, false);
		infoState.clickSelect(1, true);
		infoState.toggleTrackInfo(table(ids));
		assert.deepEqual(infoState.trackInfoSnapshot(table(ids)), { open: true, deck: 1, stable_id: 'a' });
		infoState.closeTrackInfo();
		infoState.clickSelect(1, false);
		infoState.toggleTrackInfo(table(ids, 2));
		// The record stays on the deck and track it was opened for, then closes when that track leaves.
		assert.deepEqual(infoState.shownTrackInfo(table(ids, 2)), { deckId: 1, stableId: 'a' });
		assert.equal(infoState.shownTrackInfo(table({ 1: 'z', 2: 'b' }, 2)), null);
		infoState.toggleTrackInfo(table(ids, 2));
		assert.equal(infoState.trackInfo.opened, null, 'a second toggle closes');
		assert.throws(() => infoState.toggleTrackInfo(table({})), /no deck holds a track/);
		assert.equal(infoState.trackInfo.opened, null, 'a refused open leaves it closed');
		// Control: with deck 1 empty, the open falls back to the master.
		infoState.toggleTrackInfo(table({ 2: 'b' }, 2));
		assert.deepEqual(infoState.trackInfo.opened, { deckId: 2, stableId: 'b' });
		infoState.closeTrackInfo();
		assert.equal(infoState.trackInfo.opened, null);
	});

	test('a track info chunk that fails to load is shown, not rethrown (Sol P2 4302079681)', () => {
		// Driving a real chunk fetch failure needs a browser and a deploy that
		// removes the chunk, so the failure branch is pinned in the source: it
		// closes the overlay, keeps the failure with reload guidance, renders it
		// as an alert, and does not rethrow into an unhandled rejection.
		const button = read('lib/components/rb/TrackInfoHost.svelte');
		const failure = button.match(/\(err: unknown\) => \{([\s\S]*?)\n\t\t\t\}/);
		assert.ok(failure, 'the import has a rejection handler');
		assert.match(failure[1], /closeTrackInfo\(\);/);
		assert.match(failure[1], /loadFailure = `Track information failed to load: .*\. Reload the page to retry\.`;/);
		assert.doesNotMatch(failure[1], /throw/);
		assert.match(button, /\{#if loadFailure !== null\}\s*<span class="tb-load-failure" role="alert"[^>]*>\{loadFailure\}<\/span\s*>/);
		assert.match(button, /if \(shown === null \|\| Overlay !== null\) return;\s*loadFailure = null;/);
	});

	it('Last played shows the later of the Open DJ and record timestamps (Sol P2 4287872624)', () => {
		const record = { stable_id: 'abc', last_played_at: '2026-10-05T12:00:00Z', provenance: {} };
		const last = (opendj) =>
			Object.fromEntries(
				info
					.trackInfoRows(record, { opendj_play_count: 1, rekordbox_play_count: 1, opendj_last_played_at: opendj })
					.map((r) => [r.label, r.value])
			)['Last played'];
		// if the Open DJ stamp always wins, a newer rekordbox play shows as the older one - broken.
		assert.equal(last('2026-10-01T09:00:00Z'), '2026-10-05T12:00:00Z');
		// control: a newer Open DJ play still wins, so the fix is not "prefer the record".
		assert.equal(last('2026-10-09T09:00:00Z'), '2026-10-09T09:00:00Z');
		assert.equal(last(null), '2026-10-05T12:00:00Z');
		// A stamp that does not parse is surfaced, never shown as a play or hidden
		// behind the other one (Sol P1 4301475296).
		assert.equal(last('not a date'), `${info.UNREADABLE_TIMESTAMP} (not a date)`);
		const only = (stamp) =>
			Object.fromEntries(
				info
					.trackInfoRows({ stable_id: 'abc', last_played_at: stamp, provenance: {} }, null)
					.map((r) => [r.label, r.value])
			)['Last played'];
		assert.equal(only('garbage'), `${info.UNREADABLE_TIMESTAMP} (garbage)`, 'the only stamp is not shown as a play either');
		assert.equal(only(null), info.NOT_SET, 'control: no stamp at all is still "not set"');
	});

	it('rows say "not set" for missing fields and carry provenance hovers', () => {
		const rows = info.trackInfoRows(
			{
				stable_id: 'abc',
				title: 'Song',
				artist: null,
				bpm: 124,
				key: '8A',
				duration_ms: 245000,
				tags: [],
				play_count: 3,
				created_at: '2026-10-01T00:00:00Z',
				updated_at: '2026-10-02T00:00:00Z',
				provenance: { key: { source: 'mik', confidence: 0.9, modified_at: '2026-10-02', status: 'ok' } }
			},
			{ opendj_play_count: 2, rekordbox_play_count: 7, opendj_last_played_at: null }
		);
		const by = Object.fromEntries(rows.map((r) => [r.label, r]));
		assert.equal(by.Artist.value, info.NOT_SET);
		assert.equal(by.BPM.value, '124.00');
		assert.equal(by.Duration.value, '4:05');
		assert.equal(by.Key.provenance, 'source mik, confidence 0.9, set 2026-10-02');
		assert.equal(by.Title.provenance, null);
		assert.equal(by.Plays.value, '2 in Open DJ, 7 in rekordbox');
		assert.equal(by.Tags.value, info.NOT_SET);
		assert.equal(by.Updated.value, '2026-10-02T00:00:00Z', 'updated_at has its own row');
		assert.equal(by.Added.value, '2026-10-01T00:00:00Z');
	});

	it('without a plays read, Plays reads "not set", never the record\'s default 0 (Sol P1 4290983938, Codex P2 4291070831)', () => {
		const base = { stable_id: 'abc', title: 'Song', created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-02T00:00:00Z' };
		const plays = (record, read) => Object.fromEntries(info.trackInfoRows(record, read).map((r) => [r.label, r.value])).Plays;
		// GET /api/v1/tracks/{id} leaves play_count at TrackOut's default 0, so a
		// record count is not evidence of plays when the plays read failed.
		assert.equal(plays({ ...base, play_count: 0 }, null), info.NOT_SET, 'the default 0 must not read as a real count');
		assert.equal(plays(base, null), info.NOT_SET);
		// Control in the other direction: a plays read that answered shows its counts, zeros included.
		assert.equal(plays({ ...base, play_count: 0 }, { opendj_play_count: 0, rekordbox_play_count: 0, opendj_last_played_at: null }), '0 in Open DJ, 0 in rekordbox');
	});

	it('every TrackOut field the API sends has a row (or is the provenance hover), none twice', () => {
		// The key set comes from the API contract itself (openapi.json TrackOut),
		// not a hand list, so a field the API adds fails here until it has a row.
		const spec = JSON.parse(readFileSync(new URL('../../../openapi.json', import.meta.url), 'utf8'));
		const apiKeys = Object.keys(spec.components.schemas.TrackOut.properties).sort();
		assert.ok(apiKeys.includes('stems_available'), 'control: the contract lists a field the old hand type left out');
		assert.deepEqual(Object.keys(info.TRACK_INFO_FIELD_ROWS).sort(), apiKeys);
		const record = {
			stable_id: 'abc',
			title: 't',
			artist: 'a',
			album: 'al',
			bpm: 120,
			key: '1A',
			key_source: 'own',
			key_guess: '8A',
			key_guess_confidence: 0.42,
			duration_ms: 1000,
			rating: 3,
			color_id: 2,
			tags: ['x'],
			notes: 'n',
			comments: 'c',
			file_path: '/Volumes/GIG-A/Contents/t.mp3',
			play_count: 1,
			last_played_at: '2026-10-03T00:00:00Z',
			created_at: '2026-10-01T00:00:00Z',
			updated_at: '2026-10-02T00:00:00Z',
			tempo_pref: { regular: 124, min: 120, max: 128 },
			has_rb_mapping: true,
			artwork_available: false,
			lyrics_available: true,
			auto_cues_available: false,
			stems_available: true,
			tracklist_hidden: false,
			loudness: { lufs: -9.2, status: 'ok', reason: null },
			provenance: {}
		};
		assert.deepEqual(Object.keys(record).sort(), apiKeys, 'the sample carries every API field');
		assert.equal(info.TRACK_INFO_FIELD_ROWS.provenance, null, 'provenance is each row\'s hover, not a row');
		const rows = info.trackInfoRows(record, { opendj_play_count: 1, rekordbox_play_count: 0, opendj_last_played_at: null });
		assert.equal(rows.length, apiKeys.length - 1, 'one row per field except provenance');
		assert.equal(new Set(rows.map((r) => r.label)).size, rows.length, 'no label repeats');
		for (const r of rows) assert.notEqual(r.value, info.NOT_SET, `${r.label} dropped a value the record carries`);
		const by = Object.fromEntries(rows.map((r) => [r.label, r.value]));
		assert.equal(by['Preferred tempo'], 'preferred 124.00, min 120.00, max 128.00');
		assert.equal(by.Loudness, '-9.2 LUFS', 'AGENT-31: integrated loudness');
		assert.equal(by.Stems, 'yes');
		assert.equal(by.Artwork, 'no', 'false is a value, not "not set"');
		assert.equal(by['Hide in tracklists'], 'no', 'NOWP-11487: tracklist_hidden');
	});

	it('a USB stick track reads its grid provenance off the deck ANLZ, never the library route', () => {
		const id = 'usb-0A1B-42';
		const anlz = (beats) => ({
			stable_id: id,
			beatgrid_source: 'rekordbox',
			beatgrid: { source: 'rekordbox', beat_count: beats, beats: [] }
		});
		const loaded = gridProv.usbGridProvenance(id, { stable_id: id, anlz: anlz(512), anlz_error: null });
		assert.equal(loaded.value, 'rekordbox analysis (USB stick)');
		assert.match(loaded.title, /USB stick's ANLZ that this deck loaded \(512 beats\)/);
		assert.equal(
			gridProv.usbGridProvenance(id, { stable_id: id, anlz: anlz(0), anlz_error: null }).value,
			'none on the USB stick'
		);
		const failed = gridProv.usbGridProvenance(id, { stable_id: id, anlz: null, anlz_error: 'ANALYSIS_NOT_FOUND' });
		assert.equal(failed.value, 'unavailable');
		assert.match(failed.title, /ANALYSIS_NOT_FOUND/);
		const pending = gridProv.usbGridProvenance(id, { stable_id: id, anlz: null, anlz_error: null });
		assert.equal(pending.value, 'unavailable');
		assert.match(pending.title, /has not loaded on the deck yet/);
		const noDeck = gridProv.usbGridProvenance(id, undefined);
		assert.equal(noDeck.value, 'unavailable', 'a row-opened overlay with no deck never guesses');
		assert.match(noDeck.title, /has not loaded on the deck yet/);
		// Another track's ANLZ still on the deck is not this track's provenance.
		const stale = { ...anlz(512), stable_id: 'usb-0A1B-7' };
		assert.equal(gridProv.usbGridProvenance(id, { stable_id: id, anlz: stale, anlz_error: null }).value, 'unavailable');
		assert.equal(gridProv.usbGridProvenance(id, { stable_id: 'usb-0A1B-7', anlz: anlz(512), anlz_error: null }).value, 'unavailable');
		assert.throws(
			() => gridProv.usbGridProvenance(id, { stable_id: id, anlz: { ...anlz(8), beatgrid_source: 'own' }, anlz_error: null }),
			/a stick has only its rekordbox grid/
		);
		const overlay = read('lib/components/rb/TrackInfoOverlay.svelte');
		assert.match(overlay, /if \(!isUsbTrackId\(id\)\) requestGridProvenance\(id\);/);
		assert.equal(overlay.match(/requestGridProvenance\(/g).length, 1, 'one guarded call site only');
		assert.match(
			overlay,
			/isUsbTrackId\(stableId\)\s*\?\s*usbGridProvenance\(stableId, deckId !== null \? deckStates\[deckId\] : undefined\)/
		);
		assert.match(overlay, /<dd data-testid="track-info-grid" title=\{grid\.title\}>\{grid\.value\}<\/dd>/);
		assert.match(overlay, /:\s*libraryGridProvenance\(gridProvenanceFor\(stableId\)\)/);
		// The load effect tracks stableId only: the provenance store read inside
		// _load must not re-run it (and refetch both GETs) when that request lands.
		assert.match(overlay, /const id = stableId;\s*untrack\(\(\) => void _load\(id\)\);/);
		assert.doesNotMatch(overlay, /\$effect\(\(\) => \{\s*void _load\(stableId\);/);
	});

	test('a refused two-track AutoPlay toggle puts its checkbox back', () => {
		const block = TOPBAR.slice(TOPBAR.indexOf('data-testid="ap-two-track"'), TOPBAR.indexOf('Two-track AutoPlay (decks 1 and 2 only)'));
		assert.match(block, /const result = await runPerformanceCommandFromUi\(\{\s*type: 'auto_play_two_track'/);
		assert.match(block, /if \(!result\.ok\) input\.checked = uiPrefs\.auto_play_two_deck;/);
	});

	test('the top bar mounts the live button in the evictable utility slot', () => {
		assert.match(TOPBAR, /<span class="info-slot topbar-slot-utility"\s*><TrackInfoButton\b[\s\S]*?\/><\/span\s*>/);
		assert.doesNotMatch(TOPBAR, /plannedTitle\('information'\)/);
	});

	test('the overlay and its load failure mount outside the evictable utility slot (Sol P1 4350988015)', () => {
		// The width tiers and LESS view hide `.topbar-slot-utility` with
		// display:none, which hides a position:fixed descendant too, while
		// track_info_toggle still opens the record there.
		const slot = TOPBAR.match(/<span class="info-slot topbar-slot-utility"\s*>([\s\S]*?)<\/span\s*>/);
		assert.ok(slot, 'the utility slot is in the top bar');
		assert.doesNotMatch(slot[1], /TrackInfoHost|TrackInfoOverlay|loadFailure/);
		assert.match(TOPBAR.slice(TOPBAR.indexOf(slot[0]) + slot[0].length), /^\s*<TrackInfoHost\b/);
		const button = read('lib/components/rb/TrackInfoButton.svelte');
		assert.doesNotMatch(button, /TrackInfoOverlay|loadFailure|role="alert"/, 'the overlay or its alert moved back into the slot');
		const host = read('lib/components/rb/TrackInfoHost.svelte');
		assert.match(host, /import\('\.\/TrackInfoOverlay\.svelte'\)/);
		assert.match(host, /\.tb-load-failure \{\s*position: fixed;/);
	});
});

describe('TOPVIEW-06: two-track AutoPlay keeps to decks 1 and 2', () => {
	const snap = (id, partial = {}) => ({
		id,
		stable_id: null,
		playing: false,
		position_ms: 0,
		duration_ms: null,
		is_master: false,
		beat_sync_enabled: false,
		...partial
	});

	it('hands off to the other of decks 1 and 2, never 3 or 4', () => {
		const decks = [snap(1, { playing: true, stable_id: 'a' }), snap(2, { stable_id: 'b' }), snap(3), snap(4)];
		assert.equal(autoPlay.pickFollowerDeck(decks, 1, true), 2);
		// Partner playing: waits rather than reaching for the empty deck 3.
		const busy = [snap(1, { playing: true, stable_id: 'a' }), snap(2, { playing: true, stable_id: 'b' }), snap(3), snap(4)];
		assert.equal(autoPlay.pickFollowerDeck(busy, 1, true), null);
	});

	it('from a master on deck 3 it prefers an empty deck 1 or 2 over a stopped one', () => {
		const decks = [snap(1, { stable_id: 'x' }), snap(2), snap(3, { playing: true, stable_id: 'a' }), snap(4)];
		assert.equal(autoPlay.pickFollowerDeck(decks, 3, true), 2);
	});

	it('off, the PLAY-22 four-deck choice is unchanged', () => {
		const busy = [snap(1, { playing: true, stable_id: 'a' }), snap(2, { playing: true, stable_id: 'b' }), snap(3), snap(4)];
		assert.equal(autoPlay.pickFollowerDeck(busy, 1, false), 3);
	});

	it('refuses a call that omits twoDeck rather than defaulting to four decks', () => {
		const busy = [snap(1, { playing: true, stable_id: 'a' }), snap(2, { playing: true, stable_id: 'b' }), snap(3), snap(4)];
		assert.throws(() => autoPlay.pickFollowerDeck(busy, 1), /twoDeck must be the auto_play_two_deck pref/);
		assert.throws(() => autoPlay.pickFollowerDeck(busy, 1, 'yes'), /twoDeck must be/);
	});
});

describe('track info overlay: no library-only page link for a stick track (Sol P2 4292579888)', () => {
	const OVERLAY = read('lib/components/rb/TrackInfoOverlay.svelte');
	it('the full-page link renders only for a library track', () => {
		assert.match(
			OVERLAY,
			/\{#if !isUsbTrackId\(stableId\)\}[\s\S]*?<a class="full-page" href=\{`\/track\/\$\{encodeURIComponent\(stableId\)\}`\}>[\s\S]*?\{\/if\}/
		);
		assert.equal((OVERLAY.match(/class="full-page"/g) ?? []).length, 1, 'no second, unguarded link');
	});
});
