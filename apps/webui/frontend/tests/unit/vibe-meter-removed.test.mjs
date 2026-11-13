// R10 (the maintainer, Wed 7 Oct 2026): "remove the vibe meter. keep the two thumbs for now".
// [if] the vibe meter, its mouse-movement charge or its rainbow return [then] this fails, [else stop].
// [if] the two thumbs stop rendering in the TopBar centre slot, or stop firing feedback_mark [then] this fails, [else stop].
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const SRC = fileURLToPath(new URL('../../src/', import.meta.url));
const read = (rel) => readFileSync(`${SRC}${rel}`, 'utf8');

let ssr;
before(async () => {
	ssr = await loadSvelteSsrModule(
		"export { default as TopBarThumbs } from '$lib/components/rb/FeedbackThumbs.svelte';"
	);
});

/** The one place the meter's absence is decided, so the mutation control can reuse it. */
function meterPresent(topbar, thumbs) {
	return (
		/role="meter"[^>]*aria-label="vibe"|aria-label="vibe"/.test(thumbs) ||
		/VibeMeter|vibeState|vibe-rainbow|startVibeMeter/.test(topbar + thumbs)
	);
}

test('R10: the vibe meter, its module and its rainbow are gone', () => {
	assert.equal(existsSync(`${SRC}lib/components/rb/VibeMeter.svelte`), false);
	assert.equal(existsSync(`${SRC}lib/rb/vibe.svelte.ts`), false);
	assert.equal(meterPresent(read('lib/components/rb/TopBar.svelte'), read('lib/components/rb/FeedbackThumbs.svelte')), false);
});

test('R10 mutation control: the old meter markup is detected', () => {
	const oldThumbs = '<div class="vibe" role="meter" aria-label="vibe"></div>';
	const oldTopbar = '<header class:vibe-rainbow={vibeState.display >= 0.9}><VibeMeter /></header>';
	assert.equal(meterPresent(oldTopbar, oldThumbs), true, 'the absence check cannot see a meter, so it proves nothing');
});

test('R10: both thumbs render in the TopBar centre slot and fire their existing handlers', async () => {
	const topbar = read('lib/components/rb/TopBar.svelte');
	assert.match(topbar, /<div\s+class="thumbs-slot topbar-slot-thumbs"[^>]*>\s*<FeedbackThumbs \/>/);
	const { render } = await import('svelte/server');
	const html = render(ssr.TopBarThumbs, {}).body;
	assert.match(html, /aria-label="thumbs down"/);
	assert.match(html, /aria-label="thumbs up"/);
	assert.doesNotMatch(html, /role="meter"/);
	const thumbs = read('lib/components/rb/FeedbackThumbs.svelte');
	assert.match(thumbs, /onclick=\{\(event\) => recordFeedback\('bad', event\)\}/);
	assert.match(thumbs, /onclick=\{\(event\) => recordFeedback\(event\.shiftKey \? 'great' : 'good', event\)\}/);
	assert.match(thumbs, /runPerformanceCommandFromUi\(\{ type: 'feedback_mark', vote \}, event\.timeStamp\)/);
});

test('R10: feedback recording survives the split out of vibe.svelte.ts', () => {
	const fb = read('lib/rb/performance-feedback.ts');
	for (const fn of ['hydratePerformanceFeedback', 'recordPerformanceFeedback', 'performanceFeedbackSummary']) {
		assert.match(fb, new RegExp(`export (async )?function ${fn}\\(`));
	}
	assert.match(read('lib/rb/performance-ipc.svelte.ts'), /from '\$lib\/rb\/performance-feedback'/);
	assert.match(read('routes/performance/+page.svelte'), /from '\$lib\/rb\/performance-feedback'/);
});
