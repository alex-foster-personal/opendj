/**
 * Health-light rules (HEALTH-01, HEALTH-03, HEALTH-04): what makes each dot
 * green, amber or grey. Imports and calls the exact pure module
 * BrowserPanel.svelte runs - no source slicing, no reconstructed copy.
 *
 * Regression lines:
 *   - if off-machine rows turn the Library health dot amber then broken
 *   - if a genuinely broken local link leaves the dot green then broken
 *   - if a terminal ("no lyrics available") track keeps a dot amber then broken
 *   - if a pending track lets a dot go green then broken
 *   - if an endpoint that cannot answer renders a verdict instead of grey then broken
 *   - if a dot quotes a count without naming the `present` denominator then broken
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let dots;

before(async () => {
	dots = await loadTypeScriptModule('src/lib/rb/library-health-dots.ts');
});

function availability(overrides = {}) {
	const base = {
		total: 10005,
		present: 2471,
		broken_here: 0,
		off_machine: 7208,
		awaiting_volume: 10,
		streaming: 315,
		pathless: 1
	};
	return { ...base, ...overrides };
}

function coverage(step, counts, extra = {}) {
	const { done = 0, terminal = 0, failed = 0, pending = 0, corrupt = 0 } = counts;
	return {
		on_disk: done + terminal + failed + pending,
		done: { [step]: done },
		terminal: { [step]: terminal },
		failed: { [step]: failed },
		pending: { [step]: pending },
		corrupt: { [step]: corrupt },
		waiting_on_stems: 0,
		stems_source_refusal: null,
		local: { stems: step === 'stems' ? done : 0 },
		in_cloud: { stems: 0 },
		awaiting_stem_download: 0,
		stems_index: { state: 'ok', reason: null },
		age_s: 0,
		refreshing: false,
		refresh_error: null,
		...extra
	};
}

// REQ: HEALTH-01
test('Library health is green when every track expected on this machine resolves, whatever lives elsewhere', () => {
	const dot = dots.libraryHealthDot(null, 10005, 2, availability(), null);
	assert.equal(dot.state, 'complete');
	assert.match(dot.detail, /2471 of 2471 tracks expected on this machine resolve/);
	assert.match(dot.detail, /denominator: present/, 'the denominator must be named');
	assert.match(dot.detail, /7208 on other machines/, 'off-machine rows are reported, not hidden');
	assert.match(dot.detail, /10 awaiting a volume/, 'awaiting-volume is its own count');
	assert.doesNotMatch(dot.detail, /10005/, 'the raw row count is never quoted as a total');
});

// REQ: HEALTH-01
test('overshoot control: a link that should resolve on this machine and does not keeps the dot amber', () => {
	const dot = dots.libraryHealthDot(null, 10005, 2, availability({ present: 2468, broken_here: 3 }), null);
	assert.equal(dot.state, 'incomplete');
	assert.match(dot.detail, /2468 of 2471 tracks expected on this machine resolve/);
	assert.match(dot.detail, /3 broken links here/);
});

// REQ: HEALTH-01
test('Library health is grey unknown, never a verdict, when the summary cannot answer', () => {
	for (const dot of [
		dots.libraryHealthDot(null, 10005, 2, null, 'reconcile summary request timed out'),
		// A LATER refresh failed after an earlier one settled: stale counts must go.
		dots.libraryHealthDot(null, 10005, 2, availability(), 'reconcile summary request timed out'),
		dots.libraryHealthDot(null, 10005, 2, 'unknown', null),
		dots.libraryHealthDot(null, 10005, 2, { total: 5, present: 'many' }, null)
	]) {
		assert.equal(dot.state, 'unavailable');
		assert.match(dot.detail, /^unknown/);
		assert.doesNotMatch(dot.detail, /2471/);
	}
});

test('Library health is loading, not a verdict, before the first summary lands', () => {
	const dot = dots.libraryHealthDot(null, 10005, 2, null, null);
	assert.equal(dot.state, 'loading');
	assert.doesNotMatch(dot.detail, /10005/);
});

test('an empty expected-here set is grey: there is nothing to be green about', () => {
	const dot = dots.libraryHealthDot(null, 40, 1, availability({ total: 40, present: 0, off_machine: 40, awaiting_volume: 0, streaming: 0, pathless: 0 }), null);
	assert.equal(dot.state, 'unavailable');
	assert.match(dot.detail, /no tracks are expected on this machine/);
});

// REQ: HEALTH-04
test('a coverage dot is green when nothing is pending: done and terminal both count as finished', () => {
	const dot = dots.coverageDot('Lyrics completion', coverage('lyrics', { done: 900, terminal: 84 }), 'lyrics');
	assert.equal(dot.state, 'complete');
	assert.match(dot.detail, /900 done/);
	assert.match(dot.detail, /84 no lyrics available/);
	assert.match(dot.detail, /0 pending/);
	assert.match(dot.detail, /of 984 present tracks/, 'the denominator must be named `present`');
});

// REQ: HEALTH-04
test('overshoot control: one pending track keeps a coverage dot amber', () => {
	const dot = dots.coverageDot('Lyrics completion', coverage('lyrics', { done: 900, terminal: 84, pending: 1 }), 'lyrics');
	assert.equal(dot.state, 'incomplete');
	assert.match(dot.detail, /1 pending/);
});

// REQ: HEALTH-04
test('a terminally failed job keeps the dot amber and is counted on its own', () => {
	const dot = dots.coverageDot('Vocals completion', coverage('vocals', { done: 10, failed: 2 }), 'vocals');
	assert.equal(dot.state, 'incomplete');
	assert.match(dot.detail, /2 failed/);
});

// REQ: HEALTH-07
test('evicted stems are done: the Stems dot is green and the hover splits local from in cloud', () => {
	const dot = dots.coverageDot(
		'Stems completion',
		coverage('stems', { done: 1139, terminal: 45 }, { local: { stems: 602 }, in_cloud: { stems: 537 } }),
		'stems'
	);
	assert.equal(dot.state, 'complete');
	assert.match(dot.detail, /602 local, 537 in cloud \(fetched back when loaded on a deck\)/);
	assert.match(dot.detail, /45 no stems source, 0 pending/);
	assert.match(dot.detail, /of 1184 present tracks \(denominator: present/);
});

// REQ: HEALTH-07
test('overshoot control: a track in neither place keeps the Stems dot amber beside in-cloud ones', () => {
	const dot = dots.coverageDot(
		'Stems completion',
		coverage('stems', { done: 10, pending: 1 }, { local: { stems: 4 }, in_cloud: { stems: 6 } }),
		'stems'
	);
	assert.equal(dot.state, 'incomplete');
	assert.match(dot.detail, /4 local, 6 in cloud/);
	assert.match(dot.detail, /1 pending/);
});

// REQ: HEALTH-07
test('an unreadable cloud index with tracks lacking local stems is grey: not amber, not green', () => {
	const unreadable = { state: 'unknown', reason: 'the R2 stem index cache cannot be read: bad JSON' };
	const dot = dots.coverageDot(
		'Stems completion',
		coverage('stems', { done: 602, pending: 582 }, { stems_index: unreadable }),
		'stems'
	);
	assert.equal(dot.state, 'unavailable');
	assert.match(dot.detail, /^unknown - 582 tracks have no local stems and the cloud stem index could not be read/);
	assert.match(dot.detail, /cannot be read: bad JSON/);
	assert.match(dot.detail, /602 local of 1184 present tracks/);
	// Control the other way: with nothing pending the verdict does not depend
	// on the index, so an unreadable index must not grey a finished light.
	const finished = dots.coverageDot(
		'Stems completion',
		coverage('stems', { done: 602 }, { stems_index: unreadable }),
		'stems'
	);
	assert.equal(finished.state, 'complete');
});

// REQ: HEALTH-07
test('stem counts that do not add up, or a missing index verdict, are grey unknown', () => {
	const short = dots.coverageDot(
		'Stems completion',
		coverage('stems', { done: 10 }, { local: { stems: 4 }, in_cloud: { stems: 5 } }),
		'stems'
	);
	assert.equal(short.state, 'unavailable');
	assert.match(short.detail, /do not sum to done/);
	const response = coverage('stems', { done: 10 });
	delete response.stems_index;
	assert.equal(dots.coverageDot('Stems completion', response, 'stems').state, 'unavailable');
	// Lyrics do not depend on the stem index at all.
	const lyrics = coverage('lyrics', { done: 3 });
	delete lyrics.stems_index;
	assert.equal(dots.coverageDot('Lyrics completion', lyrics, 'lyrics').state, 'complete');
});

// REQ: HEALTH-08
test('vocals hover separates a stem download from a stem render', () => {
	const dot = dots.coverageDot(
		'Vocals completion',
		coverage('vocals', { done: 720, terminal: 45, pending: 419 }, { awaiting_stem_download: 287 }),
		'vocals'
	);
	assert.equal(dot.state, 'incomplete');
	assert.match(dot.detail, /419 pending \(287 with stems in cloud, fetched one at a time\)/);
	assert.doesNotMatch(dot.detail, /waiting on stems/);
	const unread = dots.coverageDot(
		'Vocals completion',
		coverage('vocals', { done: 1, pending: 3 }, {
			waiting_on_stems: 3,
			stems_index: { state: 'unknown', reason: 'not fetched' }
		}),
		'vocals'
	);
	assert.equal(unread.state, 'incomplete', 'pending vocals are pending whatever the index says');
	assert.match(unread.detail, /cloud stem index could not be read \(not fetched\)/);
});

// REQ: HEALTH-04
test('stems and vocals name their terminal state and what is waiting on the farm', () => {
	const stems = dots.coverageDot('Stems completion', coverage('stems', { done: 5, terminal: 1, pending: 3 }), 'stems');
	assert.match(stems.detail, /1 no stems source/);
	assert.match(stems.detail, /3 pending/);
	const vocals = dots.coverageDot(
		'Vocals completion',
		coverage('vocals', { done: 5, terminal: 1, pending: 3 }, { waiting_on_stems: 3 }),
		'vocals'
	);
	assert.match(vocals.detail, /3 pending \(3 waiting on stems\)/);
});

test('a corrupt cache entry is still a visible error, never a quiet amber', () => {
	const dot = dots.coverageDot('Lyrics completion', coverage('lyrics', { done: 2, pending: 1, corrupt: 1 }), 'lyrics');
	assert.equal(dot.state, 'error');
	assert.match(dot.detail, /^1 corrupt entry/);
});

// REQ: HEALTH-03
test('a coverage endpoint that cannot answer is grey unknown, never a verdict', () => {
	const dot = dots.unknownDot('Stems completion', 'coverage request timed out after 15 s');
	assert.deepEqual(dot, {
		label: 'Stems completion',
		state: 'unavailable',
		detail: 'unknown - coverage request timed out after 15 s'
	});
});

// REQ: HEALTH-03
for (const [name, mutate] of [
	['a missing count', (c) => delete c.pending.lyrics],
	['a negative count', (c) => (c.done.lyrics = -1)],
	['a fractional count', (c) => (c.corrupt.lyrics = 0.5)],
	['states that do not sum to present', (c) => (c.on_disk += 1)]
]) {
	test(`${name} in the coverage response is grey unknown, not a verdict`, () => {
		const response = coverage('lyrics', { done: 3 });
		mutate(response);
		const dot = dots.coverageDot('Lyrics completion', response, 'lyrics');
		assert.equal(dot.state, 'unavailable');
		assert.match(dot.detail, /^unknown/);
	});
}

test('no present tracks means nothing to measure: grey, not green', () => {
	const dot = dots.coverageDot('Lyrics completion', coverage('lyrics', {}), 'lyrics');
	assert.equal(dot.state, 'unavailable');
	assert.match(dot.detail, /no present tracks to measure/);
});

// ----- HEALTH-12: a cached measurement says how old it is --------------------
// Regression lines:
//   - if a dot shows counts without saying how old they are then broken
//   - if a failed refresh leaves the old counts on screen as a verdict then broken
//   - if a response that does not say its age renders a verdict then broken
//   - if the lights wait a full minute for a refresh that is already running then broken
//   - if a refresh that never settles is re-asked forever then broken

// REQ: HEALTH-12
test('a fresh measurement says it was measured just now', () => {
	const dot = dots.coverageDot('Lyrics completion', coverage('lyrics', { done: 5 }), 'lyrics');
	assert.equal(dot.state, 'complete');
	assert.match(dot.detail, /Measured just now\.$/);
});

// REQ: HEALTH-12
test('an older measurement keeps its verdict and states its age', () => {
	const dot = dots.coverageDot(
		'Lyrics completion',
		coverage('lyrics', { done: 4, pending: 1 }, { age_s: 252.4, refreshing: true }),
		'lyrics'
	);
	assert.equal(dot.state, 'incomplete', 'an old measurement is still a measurement');
	assert.match(dot.detail, /Measured 4 min ago; a fresh count is being taken\.$/);
	const settled = dots.coverageDot(
		'Lyrics completion',
		coverage('lyrics', { done: 5 }, { age_s: 12.2 }),
		'lyrics'
	);
	assert.match(settled.detail, /Measured 12 s ago\.$/);
});

// REQ: HEALTH-12
test('a failed refresh turns the dot grey instead of quoting the old counts as current', () => {
	const dot = dots.coverageDot(
		'Stems completion',
		coverage('stems', { done: 5 }, { age_s: 90, refresh_error: 'RuntimeError: state.db is locked' }),
		'stems'
	);
	assert.equal(dot.state, 'unavailable');
	assert.match(dot.detail, /^unknown - /);
	assert.match(dot.detail, /state\.db is locked/);
	assert.match(dot.detail, /1 min ago/);
});

// REQ: HEALTH-12
test('a response that does not say how old it is renders grey, not a verdict', () => {
	for (const broken of [{ age_s: undefined }, { age_s: -1 }, { age_s: 'now' }, { refreshing: undefined }]) {
		const dot = dots.coverageDot('Lyrics completion', coverage('lyrics', { done: 5 }, broken), 'lyrics');
		assert.equal(dot.state, 'unavailable', JSON.stringify(broken));
		assert.match(dot.detail, /^unknown - /);
	}
});

// REQ: HEALTH-12
test('coverage age reads in the largest sensible unit', () => {
	assert.equal(dots.coverageAgeText(0), 'just now');
	assert.equal(dots.coverageAgeText(0.9), 'just now');
	assert.equal(dots.coverageAgeText(1), '1 s ago');
	assert.equal(dots.coverageAgeText(59.9), '59 s ago');
	assert.equal(dots.coverageAgeText(60), '1 min ago');
	assert.equal(dots.coverageAgeText(3599), '59 min ago');
	assert.equal(dots.coverageAgeText(7300), '2 h ago');
});

// REQ: HEALTH-12
test('the lights re-ask soon while a refresh runs, back off, and then stand down', () => {
	const running = { ok: true, refreshing: true, refresh_error: null };
	assert.deepEqual(
		[0, 1, 2, 3, 4, 5, 6].map((n) => dots.coverageRecheckDelayMs(running, n)),
		[3000, 3000, 6000, 12000, 24000, null, null]
	);
	// Settled: nothing to re-ask until the regular refetch.
	assert.equal(dots.coverageRecheckDelayMs({ ok: true, refreshing: false, refresh_error: null }, 0), null);
	// A refresh that failed, and a request that failed, are both worth one
	// early retry rather than a full minute of grey.
	assert.equal(dots.coverageRecheckDelayMs({ ok: true, refreshing: false, refresh_error: 'x' }, 0), 3000);
	assert.equal(dots.coverageRecheckDelayMs({ ok: false }, 0), 3000);
	assert.equal(dots.coverageRecheckDelayMs({ ok: false }, 5), null);
});
