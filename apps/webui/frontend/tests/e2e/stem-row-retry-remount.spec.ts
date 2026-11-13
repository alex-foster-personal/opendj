/**
 * StemRow retry after a real client unmount/remount (STEM-48, issue #12778).
 *
 * SSR unit tests and Storybook FailedRetry prove markup and the first-mount
 * click path; neither runs destroy/recreate lifecycles. This bundles the REAL
 * StemRow.svelte and drives a parent {#if mounted} toggle plus pointer clicks
 * on the retry button.
 *
 * [if] stems failed with onLoad before remount [then] one retry click runs onLoad
 *   once [else stop].
 * [if] StemRow unmounts and remounts on the client [then] STEMS FAILED - RETRY
 *   is still a button with data-stems-action retry [else stop].
 * [if] retry is clicked after remount [then] onLoad runs again [else stop].
 */
// requirement: STEM-48
import { expect, test, type Page } from '@playwright/test';

import { bundleSvelteHarness } from './support/svelte-harness-bundle';

const HARNESS = `<script>
	import StemRow from '$lib/components/rb/deck/StemRow.svelte';

	const ctl = () => ({ muted: false, solo: false, gain: 0.5, fx_send: 0 });
	const failedDeck = {
		deck_id: 1,
		stems: {
			status: 'error',
			source: null,
			model: null,
			layout: null,
			available_controls: [],
			alignment: null,
			controls: {
				vocal: ctl(),
				instrumental: ctl(),
				drums: ctl(),
				bass: ctl(),
				other: ctl()
			},
			error: 'decode failed: bad frame',
			load: null
		}
	};

	let mounted = $state(true);
	let loadCalls = $state(0);

	const onLoad = async () => {
		loadCalls += 1;
	};
	const noop = async () => {};

	function toggleMounted() {
		mounted = !mounted;
	}
</script>

{#if mounted}
	<StemRow
		deck={failedDeck}
		pending={false}
		testIdScope="deck"
		onMute={noop}
		onSolo={noop}
		onGain={() => {}}
		{onLoad}
	/>
{/if}
<button id="toggle" type="button" onclick={toggleMounted}>toggle mount</button>
<output id="load-calls">{loadCalls}</output>`;

let script = '';

test.beforeAll(async () => {
	script = await bundleSvelteHarness(HARNESS);
});

async function mountHarness(page: Page): Promise<void> {
	// StemRow pulls wheel-adjust, whose import reads localStorage; about:blank
	// from setContent denies that access and aborts mount (CHROME harness pattern).
	await page.goto('/');
	await page.evaluate(() => {
		document.body.replaceChildren();
		document.body.style.margin = '40px';
	});
	await page.addScriptTag({ content: script });
	await expect(page.getByTestId('stem-status-deck-1')).toBeVisible();
}

test.describe('StemRow retry after client remount', () => {
	test('retry click reaches onLoad before and after unmount/remount', async ({ page }) => {
		await mountHarness(page);

		const retry = page.getByTestId('stem-status-deck-1');
		await expect(retry).toHaveRole('button');
		await expect(retry).toHaveText('STEMS FAILED - RETRY');
		await expect(retry).toHaveAttribute('data-stems-action', 'retry');

		await retry.click();
		await expect(page.locator('#load-calls')).toHaveText('1');

		await page.click('#toggle');
		await expect(retry).toHaveCount(0);

		await page.click('#toggle');
		const retryAfterRemount = page.getByTestId('stem-status-deck-1');
		await expect(retryAfterRemount).toHaveRole('button');
		await expect(retryAfterRemount).toHaveText('STEMS FAILED - RETRY');
		await expect(retryAfterRemount).toHaveAttribute('data-stems-action', 'retry');

		await retryAfterRemount.click();
		await expect(page.locator('#load-calls')).toHaveText('2');
	});
});
