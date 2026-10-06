/**
 * @pytest.mark.requirement LIBM-174
 * The library track list scrolls natively (supersedes PVPIN-19, the maintainer, Tue 6 Oct
 * 2026: "Anyone can adjust their mouse sensitivity. The lag is VERY not worth it.").
 *
 * [if] the track list is scrolled with a wheel or trackpad [then] no app wheel
 *   handler runs and the browser scrolls it [else stop].
 *
 * Regression lines:
 * - if TrackTable's scroll container gets a wheel action or listener again then
 *   wheel scrolling leaves the compositor and stalls whenever the app is busy -> broken
 */
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const path = (p) => fileURLToPath(new URL(`../../${p}`, import.meta.url));
const read = (p) => readFileSync(path(p), 'utf8');

test('the track list scroll container has no wheel handler', () => {
	const table = read('src/lib/components/rb/browser/TrackTable.svelte');
	const wrapAt = table.indexOf('class="table-wrap"');
	assert.ok(wrapAt > 0, 'table-wrap missing (control: the probe found the container)');
	const tagEnd = table.indexOf('\t>\n', wrapAt);
	assert.ok(tagEnd > wrapAt, 'end of the table-wrap tag not found (control)');
	const tag = table.slice(wrapAt, tagEnd);
	assert.match(tag, /onscroll=/, 'control: the slice is the real tag, which carries onscroll');
	assert.doesNotMatch(tag, /wheel/i, 'no use: action or handler named for the wheel');
	assert.doesNotMatch(table, /addEventListener\(\s*['"]wheel['"]/, 'no wheel listener anywhere in TrackTable');
});

test('the half-speed wheel helper is gone, not just unused', () => {
	assert.equal(existsSync(path('src/lib/rb/library-wheel-scroll.ts')), false);
	assert.doesNotMatch(read('src/lib/components/rb/browser/track-table-support.ts'), /library-wheel-scroll/);
	// Negative control: a path that does exist reports present, so the probe can say yes.
	assert.equal(existsSync(path('src/lib/rb/playing-gate.ts')), true);
});
