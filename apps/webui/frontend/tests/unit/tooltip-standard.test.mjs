/**
 * Issue #927: justified debounce, one tooltip z-layer, no native/custom overlap.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const FRONTEND = fileURLToPath(new URL('../..', import.meta.url));

test('tooltipTiming rejects a short why and accepts a long one', async () => {
	const mod = await loadTypeScriptModule('src/lib/ui/tooltip-timing.ts');
	assert.throws(() => mod.tooltipTiming({ delay: 10, why: 'short' }));
	const ok = mod.tooltipTiming({ delay: 10, why: 'long enough why' });
	assert.equal(ok.delay, 10);
});

test('named tooltip bands declare delay and why', async () => {
	const mod = await loadTypeScriptModule('src/lib/ui/tooltip-timing.ts');
	const bands = [
		'TOOLTIP_SKIM_ROW',
		'TOOLTIP_PACKED',
		'TOOLTIP_HEADPHONE_STACK',
		'TOOLTIP_HEADPHONE_CLUSTER',
		'TOOLTIP_INFREQUENT',
		'TOOLTIP_DECK_CHROME',
		'TOOLTIP_ISOLATED',
		'TOOLTIP_TRUNCATION'
	];
	for (const name of bands) {
		const band = mod[name];
		assert.ok(Number.isFinite(band.delay) && band.delay >= 0, `${name}.delay`);
		assert.ok(band.why.trim().length >= 10, `${name}.why`);
	}
	assert.equal(mod.TOOLTIP_SKIM_ROW.delay, 10);
	assert.equal(mod.TOOLTIP_PACKED.delay, 50);
	assert.equal(mod.TOOLTIP_TRUNCATION.delay, 600);
});

test('OVERLAY_Z_INDEX.tooltip clears feedback pin surfaces', async () => {
	const stack = await loadTypeScriptModule('src/lib/overlays/overlay-stack.ts');
	const z = stack.OVERLAY_Z_INDEX;
	assert.ok(z.tooltip > z.feedbackPinBubble);
	assert.ok(z.tooltip > 10060);
});

test('ControlExplainer uses tooltip.delay, overlay z, and body portal', () => {
	const source = readFileSync(
		fileURLToPath(
			new URL('../../src/lib/components/rb/deck/ControlExplainer.svelte', import.meta.url)
		),
		'utf8'
	);
	assert.doesNotMatch(source, /showDelayMs/);
	assert.doesNotMatch(source, /z-index:\s*80/);
	assert.match(source, /tooltip\.delay/);
	assert.match(source, /OVERLAY_Z_INDEX\.tooltip/);
	assert.match(source, /_portalToBody/);
});

test('TopBar feature icons use TOOLTIP_SKIM_ROW (10 ms)', () => {
	const source = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/TopBar.svelte', import.meta.url)),
		'utf8'
	);
	assert.doesNotMatch(source, /showDelayMs=\{60\}/);
	assert.match(source, /tooltip=\{TOOLTIP_SKIM_ROW\}/);
});

test('SuggestNextStrip chevron uses 50 ms TooltipTiming with flyby reason', () => {
	const source = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/SuggestNextStrip.svelte', import.meta.url)),
		'utf8'
	);
	assert.match(source, /tooltipTiming\(/);
	assert.match(source, /delay:\s*50/);
	assert.match(source, /946e04da2d0d|flyby/i);
	assert.match(source, /tooltip=\{CHEVRON_EXPLAIN_DELAY_MS\}/);
});

const OVERLAP_FILES = [
	'../../src/lib/components/rb/TopBar.svelte',
	'../../src/lib/components/rb/mixer/HeadphoneCluster.svelte',
	'../../src/lib/components/rb/FeedbackPinShellButton.svelte',
	'../../src/lib/components/rb/FeedbackWidget.svelte',
	'../../src/lib/components/rb/deck/TransportCluster.svelte',
	'../../src/lib/components/rb/deck/JogDial.svelte',
	'../../src/lib/components/rb/deck/DeckHeader.svelte',
	'../../src/lib/components/rb/browser/StemTags.svelte',
	'../../src/lib/components/rb/browser/SearchBox.svelte',
	'../../src/routes/admin/Sparkline.svelte'
];

test('ControlExplainer triggers in the overlap sweep carry no native title', () => {
	for (const rel of OVERLAP_FILES) {
		const text = readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8');
		const blocks = text.match(/<ControlExplainer[\s\S]*?<\/ControlExplainer>/g) ?? [];
		for (const block of blocks) {
			let inner = block
				.replace(/^<ControlExplainer[^>]*>/, '')
				.replace(/<\/ControlExplainer>$/, '');
			inner = inner.replace(/\{#snippet action\(\)\}[\s\S]*?\{\/snippet\}/g, '');
			assert.doesNotMatch(
				inner,
				/\btitle=/,
				`${rel} ControlExplainer slot still has a native title attribute`
			);
			assert.doesNotMatch(inner, /<title>/, `${rel} ControlExplainer slot still has SVG title`);
		}
	}
});

test('single-hover-tooltip registers focus and uses packed delay', async () => {
	const source = readFileSync(
		fileURLToPath(new URL('../../src/lib/ui/single-hover-tooltip.ts', import.meta.url)),
		'utf8'
	);
	assert.match(source, /focusin/);
	assert.match(source, /focusout/);
	assert.match(source, /TOOLTIP_PACKED/);
});

test('ControlExplainer SSR loads and template exposes tooltip role (GUARD-15)', async () => {
	const entry = [
		"import { createRawSnippet } from 'svelte';",
		"export { default as ControlExplainer } from '$lib/components/rb/deck/ControlExplainer.svelte';",
		"export { TOOLTIP_ISOLATED } from '$lib/ui/tooltip-timing.ts';",
		"export { render } from 'svelte/server';",
		'export const trigger = createRawSnippet(() => ({ render: () => \'<button type="button">X</button>\' }));'
	].join('\n');
	const ssr = await loadSvelteSsrModule(entry);
	const body = ssr.render(ssr.ControlExplainer, {
		props: {
			title: 'Sample',
			bullets: ['One fact'],
			tooltip: ssr.TOOLTIP_ISOLATED,
			children: ssr.trigger
		}
	}).body;
	assert.match(body, /class="explainer/);
	const explainerSource = readFileSync(
		fileURLToPath(
			new URL('../../src/lib/components/rb/deck/ControlExplainer.svelte', import.meta.url)
		),
		'utf8'
	);
	assert.match(explainerSource, /role=\{showAction \? 'dialog' : 'tooltip'\}/);
	assert.match(explainerSource, /class="pop"/);
});
