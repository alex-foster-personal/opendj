// requirement: CUEOUT-28
// [if] split cue runs on one shared output [then] the TopBar badge shows only "SPLIT L/R", [else stop]
// [if] the operator hovers the badge [then] its tooltip carries the full "Split cue: master L / cue R (same device: <label>)" sentence and what to do about it, [else stop]
// [if] the badge shows [then] it sits beside the master column in the 28px top bar, never clipped under the slider and meter, [else stop]
// [if] the split starts [then] the toast and perf event still carry the full sentence, [else stop]
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const FRONTEND = fileURLToPath(new URL('../..', import.meta.url));
const BADGE = `${FRONTEND}/src/lib/components/rb/SameDeviceSplitBadge.svelte`;
const HEADPHONES = `${FRONTEND}/src/lib/player/headphones.ts`;

let collision;

before(async () => {
	collision = await loadTypeScriptModule('src/lib/player/main-cue-collision.ts');
});

/** The badge's markup between {#if label !== null} and {/if}. */
function badgeMarkup() {
	const source = readFileSync(BADGE, 'utf8');
	const start = source.indexOf('{#if label !== null}');
	const end = source.indexOf('{/if}', start);
	assert.ok(start >= 0 && end > start, 'the badge renders inside {#if label !== null}');
	return source.slice(start, end);
}

test('CUEOUT-28 the visible badge text is the compact SPLIT L/R', () => {
	assert.equal(collision.SAME_DEVICE_SPLIT_BADGE_TEXT, 'SPLIT L/R');
	const markup = badgeMarkup();
	// The only text node in the badge is the compact constant.
	const body = markup.slice(markup.indexOf('>', markup.indexOf('data-testid')) + 1).replace(/<!--[\s\S]*?-->/g, '');
	assert.match(body, /^\s*\{SAME_DEVICE_SPLIT_BADGE_TEXT\}\s*<\/div>\s*$/);
	assert.doesNotMatch(markup, /\{sameDeviceSplitText\(/, 'the long sentence is no longer the visible text');
});

test('CUEOUT-28 the hover tooltip keeps the full sentence and the fix', () => {
	const tip = collision.sameDeviceSplitTooltip('External Headphones');
	assert.ok(
		tip.startsWith('Split cue: master L / cue R (same device: External Headphones)'),
		`tooltip starts with the full sentence: ${tip}`
	);
	assert.match(tip, /Pick a different MAIN output/);
	const markup = badgeMarkup();
	assert.match(markup, /title=\{sameDeviceSplitTooltip\(label\)\}/);
	assert.match(markup, /aria-label=\{sameDeviceSplitTooltip\(label\)\}/);
});

test('CUEOUT-28 the badge mounts beside the master column, not inside it, so the top bar does not clip it', () => {
	const topbar = readFileSync(`${FRONTEND}/src/lib/components/rb/TopBar.svelte`, 'utf8');
	const start = topbar.indexOf('<div class="master-slider-wrap">');
	const mount = topbar.indexOf('<MainOutputFaultBanner />', start);
	assert.ok(start >= 0 && mount > start, 'the banner mounts after the master column opens');
	const between = topbar.slice(start, mount).replace(/<!--[\s\S]*?-->/g, '');
	const opens = (between.match(/<div\b/g) ?? []).length;
	const closes = (between.match(/<\/div>/g) ?? []).length;
	assert.equal(closes, opens, 'every div opened in the master column is closed before the banner mounts');
});
