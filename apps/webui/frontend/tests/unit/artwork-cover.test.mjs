/**
 * LIBM-185 (the maintainer, Tue 3 Nov 2026): "deck 1 has artwork but the deck 1 col does not".
 * A cover whose first load failed (online artwork not fetched yet) stayed broken.
 *
 * @pytest.mark.requirement LIBM-185
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const SITES = [
	'src/lib/components/rb/Mixer.svelte',
	'src/lib/components/rb/wave/WaveTrackSummaryView.svelte',
	'src/lib/components/rb/deck/DeckHeader.svelte',
	'src/lib/components/rb/TrackifyPlayer.svelte',
	'src/lib/components/library/TrackInspectorView.svelte',
	'src/lib/components/library/LibraryPreviewBarView.svelte',
	'src/routes/dedup/+page.svelte',
	'src/lib/components/rb/browser/TrackTable.svelte'
];

let m;
before(async () => {
	m = await loadTypeScriptModule('tests/unit/fixtures/artwork-retry-entry.ts');
});
beforeEach(() => m.resetOptionalResourcesForTests());

function fakeTimers() {
	const pending = new Map();
	let id = 1;
	return {
		setTimeout(fn, ms) {
			pending.set(id, { fn, ms });
			return id++;
		},
		clearTimeout(h) {
			pending.delete(h);
		},
		fireAll() {
			for (const [k, t] of [...pending]) {
				pending.delete(k);
				t.fn();
			}
		}
	};
}

test('if a failed cover is not retried when the library announces its track changed, it stays broken after enrichment', () => {
	const t = fakeTimers();
	const retry = new m.ArtworkRetry(t);
	const url = '/api/v1/tracks/95eb/artwork?size=s&online=true';
	retry.noteError('95eb');
	assert.equal(retry.hidden('95eb'), true, 'placeholder while waiting');
	retry.reset('95eb');
	assert.equal(retry.hidden('95eb'), false, 'reset shows the cover at once');
	assert.match(m.artworkRetrySrc(url, '95eb', retry), /&r=0\.1$/, 'new cache-buster');
});

test('if reset touches an id that never failed, it bumps a cache-buster for nothing', () => {
	const retry = new m.ArtworkRetry(fakeTimers());
	retry.reset('never-failed');
	assert.equal(m.artworkRetrySrc('/a?size=s', 'never-failed', retry), '/a?size=s');
});

test('if the backoff retry does not come with a cache-buster, the browser serves the failed response again', () => {
	const t = fakeTimers();
	const retry = new m.ArtworkRetry(t);
	retry.noteError('x');
	t.fireAll();
	assert.equal(retry.hidden('x'), false);
	assert.equal(m.artworkRetrySrc('/a?size=s', 'x', retry), '/a?size=s&r=1');
});

test('if a reset does not cancel the pending retry timer, a stale timer fires into the fresh cover', () => {
	const t = fakeTimers();
	const retry = new m.ArtworkRetry(t);
	retry.noteError('x');
	retry.reset('x');
	t.fireAll();
	assert.equal(retry.failures('x'), 0);
});

test('if an artwork img site keeps its own failed flag or lacks the expected-404 mark, it never retries or logs a client error', () => {
	for (const path of SITES) {
		const src = readFileSync(path, 'utf8');
		assert.match(
			src,
			/data-expected-resource-error="artwork-404"/,
			`${path}: 404 must not be a client error`
		);
		assert.doesNotMatch(
			src,
			/artFailed|artworkFailed/,
			`${path}: private failed flag never retries`
		);
	}
	// LibraryPreviewBarView, TrackInspectorView and WaveTrackSummaryView are
	// props-only (storybook-stories.test.mjs), so their containers own the helper
	// calls and the views only carry the expected-404 mark and the callbacks.
	for (const path of SITES.filter((p) => !p.endsWith('TrackTable.svelte'))) {
		const src = readFileSync(
			path.replace(/(LibraryPreviewBar|TrackInspector|WaveTrackSummary)View\.svelte$/, '$1.svelte'),
			'utf8'
		);
		assert.match(src, /coverFailed\(/, `${path}: error goes to the shared helper`);
		assert.match(src, /coverVisible\(/, `${path}: placeholder comes from the shared helper`);
	}
});

test('if the shared retry does not listen for track changes, covers wait for the timer instead of the enrichment event', () => {
	const src = readFileSync('src/lib/rb/artwork-retry.ts', 'utf8');
	assert.match(src, /subscribeKind\(ARTWORK_RESET_TOPIC_KIND/);
});
