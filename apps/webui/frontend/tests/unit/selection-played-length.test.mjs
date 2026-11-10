// requirement: LIBUX-10716
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

let mod;

before(async () => {
	mod = await loadSvelteSsrModule(`
		export { render } from 'svelte/server';
		export { default as SelectionPlayedLength } from '$lib/components/rb/browser/SelectionPlayedLength.svelte';
	`);
});

const row = (order, duration_ms) => ({ order, duration_ms });

test('[if] visible rows are filtered [then] the footer uses only those rows for the list line, [else stop]', () => {
	const { body } = mod.render(mod.SelectionPlayedLength, {
		props: {
			visibleRows: [row(1, 60_000), row(2, 65_000)],
			rows: [row(1, 60_000), row(2, 65_000), row(3, 999_000)],
			selectedOrders: []
		}
	});
	assert.match(body, /2 tracks, 2:05/);
	assert.doesNotMatch(body, /selected:/);
});

test('[if] some durations are null [then] the footer appends (+k unknown), [else stop]', () => {
	const { body } = mod.render(mod.SelectionPlayedLength, {
		props: {
			visibleRows: [row(1, 60_000), row(2, null)],
			rows: [row(1, 60_000), row(2, null)],
			selectedOrders: []
		}
	});
	assert.match(body, /2 tracks, 1:00 \(\+1 unknown\)/);
});

test('[if] fewer than 2 rows are selected [then] no selected suffix appears, [else stop]', () => {
	const { body } = mod.render(mod.SelectionPlayedLength, {
		props: {
			visibleRows: [row(1, 60_000), row(2, 65_000)],
			rows: [row(1, 60_000), row(2, 65_000)],
			selectedOrders: [1]
		}
	});
	assert.doesNotMatch(body, /selected:/);
});

test('[if] 2 or more rows are selected [then] the footer adds selected count and time, [else stop]', () => {
	const { body } = mod.render(mod.SelectionPlayedLength, {
		props: {
			visibleRows: [row(1, 60_000), row(2, 240_000), row(3, 100_000)],
			rows: [row(1, 60_000), row(2, 240_000), row(3, 100_000)],
			selectedOrders: [1, 2]
		}
	});
	assert.match(body, /3 tracks, 6:40 · selected: 2 tracks, 5:00/);
});
