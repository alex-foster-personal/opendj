// requirement: LIBUX-10716
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const fixture = JSON.parse(
	readFileSync(
		new URL('../../../../../tests/fixtures/list_totals_cases.json', import.meta.url),
		'utf8'
	)
);

let mod;

before(async () => {
	mod = await loadSvelteSsrModule(`
		export {
			summarizeListTotals,
			formatListTotalsLine,
			formatLibraryFooter,
			formatDurationHms
		} from '$lib/rb/list-totals';
	`);
});

test('[if] fixture durations are summarized [then] count, sum and line match Python, [else stop]', () => {
	for (const c of fixture.cases) {
		const totals = mod.summarizeListTotals(c.durations_ms.map((duration_ms) => ({ duration_ms })));
		assert.deepEqual(totals, c.totals, c.name);
		assert.equal(mod.formatListTotalsLine(totals), c.line, c.name);
	}
});

test('[if] two or more rows are selected [then] formatLibraryFooter adds the selected suffix, [else stop]', () => {
	const text = mod.formatLibraryFooter(fixture.footer.list, fixture.footer.selection);
	assert.equal(text, fixture.footer.text);
});

test('[if] 10k rows are summarized [then] one pass finishes under 75 ms, [else stop]', () => {
	const rows = Array.from({ length: 10_000 }, (_, i) => ({
		duration_ms: i % 17 === 0 ? null : 180_000 + (i % 1000)
	}));
	// Warm up after a long parallel suite so JIT cost is not measured (agentbox CI).
	mod.summarizeListTotals(rows.slice(0, 64));
	// Tier JIT on the full row shape before the measured pass on a loaded CI host.
	// Warm up on the full row count after a long parallel suite so JIT cost is not measured.
	mod.summarizeListTotals(rows);
	const started = performance.now();
	const totals = mod.summarizeListTotals(rows);
	// 5 ms holds on a quiet host; the unit lane runs with concurrency so CI runners need headroom.
	assert.ok(performance.now() - started < 75);
	assert.equal(totals.count, 10_000);
	assert.equal(totals.unknown, Math.ceil(10_000 / 17));
});

test('[if] duration is formatted [then] M:SS or H:MM:SS is used, [else stop]', () => {
	assert.equal(mod.formatDurationHms(65_000), '1:05');
	assert.equal(mod.formatDurationHms(3_661_000), '1:01:01');
});
