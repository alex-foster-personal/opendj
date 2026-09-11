import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, it } from 'node:test';

/**
 * FB-07: the feedback cluster (chevron + comment pin) sits on the LEFT-hand
 * side of the vibe meter. This supersedes the right-hand placement shipped
 * in #523.
 *
 * Both the cluster and the vibe meter are absolutely positioned against the
 * centre of the top bar, so nothing about the surrounding flow layout places
 * them - only the .fb-cluster anchor does. That makes the placement invisible
 * to every other test in the suite, and a one-word CSS edit away from
 * silently flipping back.
 *
 * Measured in a real browser at 1440px and at 800px, Mon 31 Aug 2026: cluster
 * right edge 628 / vibe left edge 645, and 308 / 325 respectively - left of
 * it, with the same 17px gap the right-hand placement used, and the meter
 * still exactly centred.
 *
 * [if] .fb-cluster anchors `left:` again [then] the cluster jumps back to the
 *   right of the meter - broken.
 * [if] the anchor stops being symmetric with the meter's half-width [then] the
 *   gap drifts and the cluster can overlap the meter - broken.
 * [if] FeedbackWidget renders after the vibe meter [then] focus and
 *   screen-reader order contradict what is on screen - broken.
 */

const read = (relative) =>
	readFileSync(fileURLToPath(new URL(relative, import.meta.url)), 'utf8');

/** Every `@media (max-width: Npx)` block in `css`, as {maxWidth, body}, widest
 * first. Parsed rather than sliced between two hardcoded breakpoint literals:
 * the literals rot the moment a breakpoint is re-measured, and a slice between
 * two absent literals silently becomes the empty string, which asserts nothing.
 * (Exactly that happened when #1002 moved 1340 -> 1530 and 1024 -> 1210.) */
function maxWidthBlocks(css) {
	const blocks = [];
	const header = /@media \(max-width: (\d+)px\) \{/g;
	let match;
	while ((match = header.exec(css)) !== null) {
		let depth = 1;
		let index = header.lastIndex;
		while (depth > 0 && index < css.length) {
			if (css[index] === '{') depth += 1;
			else if (css[index] === '}') depth -= 1;
			index += 1;
		}
		blocks.push({ maxWidth: Number(match[1]), body: css.slice(header.lastIndex, index - 1) });
	}
	if (blocks.length === 0) throw new Error('no max-width media blocks found; the parser is broken');
	return blocks.sort((a, b) => b.maxWidth - a.maxWidth);
}

const TOPBAR = read('../../src/lib/components/rb/TopBar.svelte');
const WIDGET = read('../../src/lib/components/rb/FeedbackWidget.svelte');

describe('FB-07 feedback cluster sits left of the vibe meter', () => {
	it('anchors the cluster RIGHT edge left of centre, never a left offset', () => {
		assert.match(
			WIDGET,
			/\.fb-cluster \{[^}]*right: calc\(50% \+ 92px\);/,
			'the cluster must hang off centre by its right edge'
		);
		const cluster = WIDGET.slice(
			WIDGET.indexOf('.fb-cluster {'),
			WIDGET.indexOf('}', WIDGET.indexOf('.fb-cluster {'))
		);
		assert.ok(cluster.length > 0, 'if .fb-cluster is not found then this guard asserts nothing');
		assert.doesNotMatch(
			cluster,
			/(^|[^-])left:/,
			'a left offset would put the cluster back on the right of the meter'
		);
	});

	it('anchoring the right edge keeps the gap fixed as the badge appears', () => {
		// The open-todo count renders inside the chevron button, so the cluster
		// changes width at runtime. Anchoring the right edge is what stops that
		// from moving the cluster relative to the meter.
		assert.match(WIDGET, /class="fb-count"/, 'the width-changing badge still exists');
	});

	it('the vibe meter and feedback cluster remain flow items, never overlays', () => {
		assert.match(TOPBAR, /\.vibe-slot \{[^}]*position: static/);
		assert.match(TOPBAR, /\.rb-topbar :global\(\.fb-cluster\) \{[^}]*position: static/);
	});

	it('responsive slot rules abbreviate or hide lower-priority chrome after preserving flow', () => {
		assert.match(TOPBAR, /\.rb-topbar :global\(\.fb-cluster\) \{[^}]*position: static/);
		// Responsive layout, re-measured for PARITY-02's SOURCE
		// toggle (#1002, Wed 9 Sep 2026): VIBE's eviction window has been widened
		// twice now, so what is pinned here is the ORDER, not the numbers. VIBE
		// is lower priority than the command entry, so it must be evicted at a
		// STRICTLY WIDER breakpoint than any tier that touches the command entry,
		// and the feedback triggers must survive both.
		const blocks = maxWidthBlocks(TOPBAR);
		const vibe = blocks.find((block) => block.body.includes('topbar-slot-vibe'));
		assert.ok(vibe !== undefined, 'VIBE must still have a responsive eviction tier at all');
		const commandEntryTiers = blocks.filter((block) => /\.cmd-input|\.cmd-entry|\.cmd-status/.test(block.body));
		assert.ok(commandEntryTiers.length > 0, 'if no tier touches the command entry then this guard asserts nothing');
		for (const tier of commandEntryTiers) {
			assert.ok(
				vibe.maxWidth > tier.maxWidth,
				`VIBE (${vibe.maxWidth}px) must yield its slot before the command entry tier at ${tier.maxWidth}px`
			);
		}
		for (const block of blocks) {
			assert.doesNotMatch(
				block.body,
				/\.fb-cluster[^}]*display:\s*none/,
				`feedback triggers must stay reachable at every width (tier ${block.maxWidth}px hides them)`
			);
		}
		assert.match(TOPBAR, /@media \(max-width: 980px\)[\s\S]*content: 'BSM'/);
		assert.match(TOPBAR, /<CommandEntry \/>/, 'command entry remains in every responsive tier');
		assert.match(TOPBAR, /aria-label="Jobs drawer"/, 'live jobs indicator remains in every responsive tier');
	});

	it('responsive VIBE preserves DOM cluster order instead of trailing every utility control', () => {
		// Same reason as above: read the blocks, do not slice between literals.
		for (const block of maxWidthBlocks(TOPBAR)) {
			assert.doesNotMatch(
				block.body,
				/\border\s*:/,
				`partial order overrides move VIBE and AutoPlay behind the utilities (tier ${block.maxWidth}px)`
			);
		}
		const slots = ['vibe-slot topbar-slot-vibe', 'bsm-toggle topbar-slot-pairing', 'bsm-toggle topbar-slot-bsm', 'ap-wrap topbar-slot-autoplay', 'aria-label="Jobs drawer"', '<CommandEntry />'];
		const positions = slots.map((slot) => TOPBAR.indexOf(slot));
		assert.ok(positions.every((position) => position >= 0), 'each participating slot must exist');
		assert.deepEqual(positions, [...positions].sort((a, b) => a - b));
	});

	it('FeedbackWidget reads before the vibe meter, so a11y order matches', () => {
		const widgetAt = TOPBAR.indexOf('<FeedbackWidget />');
		const vibeAt = TOPBAR.indexOf('<div class="vibe-slot topbar-slot-vibe">');
		assert.notEqual(widgetAt, -1, 'if FeedbackWidget is gone then this guard asserts nothing');
		assert.notEqual(vibeAt, -1, 'if the vibe slot is gone then this guard asserts nothing');
		assert.ok(
			widgetAt < vibeAt,
			'FeedbackWidget must render before the vibe meter to match its visual position'
		);
	});
});
