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
