/**
 * Pin dd4f0f5ae33f (`/performance`, anchor a scoped chevron class, batch 9b-1).
 *
 * "check z values for tooltips - this one is a mess, and has both built-in
 * and nonnative overlapping."
 *
 * Once pin 946e04da2d0d (suggest-next-chevron-tooltip.test.mjs) gave the
 * chevron a rich ControlExplainer popover, it also kept its old native
 * `title` - so hovering showed the OS tooltip and the custom popover at
 * once, stacked on top of each other with no z-index able to fix it (a
 * native title tooltip is always OS-topmost; the only real fix is not
 * having two tooltip mechanisms live at once). The native `title` is
 * dropped; `aria-label` stays for accessibility.
 *
 * Source-level regression, matching the repo idiom for .svelte assertions.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const STRIP = fileURLToPath(
	new URL('../../src/lib/components/rb/SuggestNextStrip.svelte', import.meta.url)
);
const stripSource = readFileSync(STRIP, 'utf8');
const stripTemplate = stripSource.slice(stripSource.lastIndexOf('</script>'));

const EXPLAINER = fileURLToPath(
	new URL('../../src/lib/components/rb/deck/ControlExplainer.svelte', import.meta.url)
);
const explainerSource = readFileSync(EXPLAINER, 'utf8');

test('the chevron button keeps aria-label but drops the duplicate native title', () => {
	const playBtnBlock = stripTemplate.match(/<button[^>]*class="play-btn"[\s\S]*?<\/button>/)?.[0] ?? '';
	assert.match(playBtnBlock, /aria-label=\{_loadControlLabel\(cand, true\)\}/, 'aria-label must stay for accessibility');
	assert.doesNotMatch(playBtnBlock, /\btitle=\{/, 'a native title left on an element with a rich explainer is exactly the overlap dd4f0f5ae33f flagged');
});

test('the reused explainer popover keeps its existing z-index convention rather than a new magic number', () => {
	assert.match(explainerSource, /z-index:\s*80/, 'ControlExplainer already established z-index:80 as the in-panel popover layer elsewhere in the app (FeedbackWidget, IngestDropModal, TrackTable); reuse it rather than inventing a new value');
});
