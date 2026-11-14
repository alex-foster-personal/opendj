/**
 * Issue #13722: the edge sampler on/off control is the approved mock's 40 x 22 boxed chip
 * (variant D `.samp-toggle`), mounted beside the waveform ZOOM chip rather than in the top bar.
 *
 * Regression lines:
 * - if the performance screen renders then the chip sits in the wave area, not the top bar
 * - if the chip is clicked then sampler_enabled flips, and the accent border shows only when on
 * - if the click does not go through the performance bus then it does nothing (no command session)
 * - if the chip renders then it has the mock's box, ring, icon size and hover sentences
 *
 * SSR proves markup and scoped CSS; on-screen pixel placement still needs a browser (e2e not run).
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const stub = (name) => fileURLToPath(new URL(`./${name}`, import.meta.url));

let ssr;
before(async () => {
	ssr = await loadSvelteSsrModule(
		[
			"export { default as WaveformStack } from '$lib/components/rb/WaveformStack.svelte';",
			"export { default as TopBar } from '$lib/components/rb/TopBar.svelte';",
			"export { default as EdgeSamplerToggle } from '$lib/components/rb/sampler/EdgeSamplerToggle.svelte';",
			"export * as chip from '$lib/player/sampler/edge-sampler-toggle';",
			"export { uiPrefs } from '$lib/rb/prefs.svelte';",
			"export { installPerformanceBrowserIpc } from '$lib/rb/performance-ipc.svelte';",
			"export { toasts } from '$lib/stores.svelte';",
			"export { render } from 'svelte/server';"
		].join('\n'),
		{
			css: 'injected',
			alias: {
				'$app/stores': stub('fake-app-stores.mjs'),
				'$app/navigation': stub('fake-app-navigation.mjs')
			}
		}
	);
	// TopBar's teardown cancels a frame; SSR has no frame clock, so a no-op stands in.
	globalThis.cancelAnimationFrame ??= () => {};
});

/** Markup and scoped CSS of one server render. */
const renderOf = (component) => {
	const { head, body } = ssr.render(component, { props: {} });
	return {
		body,
		css: [...head.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map((m) => m[1]).join('\n')
	};
};

/** Declarations of the first scoped rule whose selector matches `selectorRe`. */
const rule = (css, selectorRe) => {
	const m = css.match(new RegExp(`${selectorRe.source}[^{,]*\\{([^}]*)\\}`));
	assert.ok(m, `rule ${selectorRe} present`);
	return m[1];
};

test('the performance screen mounts the chip in the wave area beside ZOOM, not in the top bar', () => {
	/** [if] the wave stack and the top bar render [then] only the wave stack carries the chip, at its top right, [else stop]. */
	const wave = renderOf(ssr.WaveformStack);
	assert.match(
		wave.body,
		/class="sampler-toggle-slot[^"]*"[^>]*data-testid="edge-sampler-toggle-slot"[^>]*>\s*(<!--[^>]*-->)?\s*<button[^>]*data-testid="edge-sampler-toggle"/
	);
	assert.equal(
		wave.body.match(/data-testid="edge-sampler-toggle"/g)?.length,
		1,
		'one chip, not one per row'
	);
	const slot = rule(wave.css, /\.sampler-toggle-slot/);
	assert.match(slot, /position:\s*absolute/);
	assert.match(slot, /top:\s*2px/);
	assert.match(rule(wave.css, /\.rb-wavestack/), /position:\s*relative/);

	const top = renderOf(ssr.TopBar).body;
	assert.match(top, /aria-label="Open settings"/, 'the top bar rendered');
	assert.doesNotMatch(top, /edge-sampler-toggle/);
});

/** One chip click with a live performance command session, as the app runs it. */
async function clickWithBus() {
	globalThis.window = {};
	const uninstall = ssr.installPerformanceBrowserIpc();
	try {
		return await ssr.chip.toggleEdgeSampler();
	} finally {
		uninstall();
		delete globalThis.window;
	}
}

test('a click with no command session changes nothing', async () => {
	/** [if] the chip is clicked before the performance bus installs [then] the pref stays and no toast shows, [else stop]. */
	ssr.uiPrefs.sampler_enabled = true;
	const toastsBefore = ssr.toasts.length;
	assert.equal(await ssr.chip.toggleEdgeSampler(), true);
	assert.equal(ssr.uiPrefs.sampler_enabled, true);
	assert.equal(ssr.toasts.length, toastsBefore);
});

test('a click flips sampler_enabled and the accent border shows only when on', async () => {
	/** [if] the chip's click runs the sampler_enabled bus command [then] the pref flips and accent shows only when on, [else stop]. */
	ssr.uiPrefs.sampler_enabled = true;
	assert.equal(await clickWithBus(), false);
	assert.equal(ssr.uiPrefs.sampler_enabled, false);
	assert.equal(ssr.toasts.at(-1)?.message, 'Edge sampler off: playing samples keep going');
	const off = renderOf(ssr.EdgeSamplerToggle);
	assert.match(off.body, /aria-pressed="false"/);
	assert.doesNotMatch(off.body, /sampler-on/);

	assert.equal(await clickWithBus(), true);
	assert.equal(ssr.uiPrefs.sampler_enabled, true);
	assert.equal(ssr.toasts.at(-1)?.message, 'Edge sampler on');
	const on = renderOf(ssr.EdgeSamplerToggle);
	assert.match(on.body, /aria-pressed="true"/);
	assert.match(on.body, /class="sampler-toggle[^"]*\bsampler-on\b/);

	const base = rule(on.css, /\.sampler-toggle/);
	const lit = rule(on.css, /\.sampler-toggle\.sampler-on/);
	assert.match(base, /border:\s*1px solid var\(--es-border-strong\)/);
	assert.match(lit, /border-color:\s*var\(--es-accent\)/);
	assert.equal(on.css.match(/var\(--es-accent\)/g)?.length, 2, 'accent only in the on rule');
});

test('the chip has the mock box, ring, icon and hover sentences', () => {
	/** [if] the chip renders [then] it is 40x22, radius 3, ringed, a 16px 1.6 stroke icon, with the mock titles, [else stop]. */
	const { body, css } = renderOf(ssr.EdgeSamplerToggle);
	const base = rule(css, /\.sampler-toggle/);
	for (const decl of [
		/width:\s*40px/,
		/height:\s*22px/,
		/border-radius:\s*3px/,
		/background:\s*var\(--es-head-bg\)/,
		/color:\s*var\(--es-muted\)/,
		/box-shadow:\s*0 0 0 2px var\(--es-toggle-ring\)/
	])
		assert.match(base, decl);
	assert.match(rule(css, /\.sampler-toggle\.sampler-on/), /background:\s*var\(--es-ghost-bg\)/);
	const icon = rule(css, /\.sampler-toggle\.svelte-\w+ svg/);
	assert.match(icon, /width:\s*16px/);
	assert.match(icon, /stroke-width:\s*1\.6/);
	assert.match(body, new RegExp(`title="${ssr.chip.samplerToggleTitle(true)}"`));
	assert.equal(
		ssr.chip.samplerToggleTitle(false),
		'Edge sampler OFF: edges stay closed, playing samples keep going. Click to turn on.'
	);
});
