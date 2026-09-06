import { test, expect } from '@playwright/test';

// E2E for the ingest feature pair (real backend on the claimed worktree
// port, isolated MDT_DATA_DIR - see PR notes):
//  * TopBar refresh-analysis button: visible, hover popover with coverage
//    counts and the analyze-on-import queue line, click starts the refresh
//    job and the popover reaches a terminal phase.
//  * Drag-in ingest modal: an external file drag opens the overlay, a drop
//    opens the modal with the config checkbox list, Stage & run uploads and
//    reports the staged file.

test.describe('refresh analysis button', () => {
	test('hover shows coverage popover and reports the real refresh result', async ({ page }) => {
		// This fixture intentionally has no Rekordbox master database or stem
		// artifacts. Configure the real ingest endpoint for its runnable analysis
		// step before exercising the top-bar control, rather than asking the
		// unrelated vocals backfill to fail on unavailable fixture data.
		const config = await page.request.put('/api/v1/ingest/config', {
			data: { enabled: { analysis: true, stems: false, vocals: false } }
		});
		expect(config.ok()).toBe(true);
		await page.goto('/performance');
		const btn = page.getByTestId('refresh-analysis');
		await expect(btn).toBeVisible({ timeout: 15_000 });
		await expect(btn).toBeEnabled();

		await btn.hover();
		const pop = page.getByTestId('refresh-analysis-pop');
		await expect(pop).toBeVisible();
		// Regression: .rb-topbar is overflow:hidden and originally clipped the
		// popover to nothing. toBeInViewport uses IntersectionObserver, which
		// reports 0 for a fully clipped element.
		await expect(pop).toBeInViewport();
		await expect(pop).toContainText('Refresh analysis');
		await expect(pop).toContainText('missing -');
		// Analyze-on-import queue: locally imported tracks with no rekordbox
		// twin. Rendered from the real /analysis-queue endpoint on the real
		// backend, so this is where the endpoint's serialization is proven end
		// to end. Assert the identity it promises survived the round trip:
		// analyzed + unreachable + pending == unmapped.
		const queueLine = page.getByTestId('analysis-queue-line');
		await expect(queueLine).toContainText('local (no rekordbox)');
		const rendered = (await queueLine.textContent()) ?? '';
		const counts = /(\d+) pending\s*·\s*(\d+) analyzed\s*·\s*(\d+) unreachable of (\d+)/.exec(
			rendered
		);
		expect(counts, `queue line did not render counts: ${rendered}`).not.toBeNull();
		const [, pending, analyzed, unreachable, unmapped] = counts!.map(Number);
		expect(pending + analyzed + unreachable).toBe(unmapped);
		expect(rendered).toMatch(/auto\s+(on|off)/);

		await btn.click();
		await expect(pop).toContainText('done', { timeout: 25_000 });
		await expect(pop).not.toContainText('error');
	});
});

test.describe('ingest drop modal', () => {
	test('file drop opens modal; stage & run uploads and reports', async ({ page }) => {
		await page.goto('/performance');
		// Hydration gate: window drag listeners attach with the component tree.
		await expect(page.getByTestId('refresh-analysis')).toBeVisible();

		// Synthesize an external file drag: DataTransfer with a File. The
		// bytes are junk mp3 (no parsable duration) - upload still stages it,
		// which is the honest v1 contract for unparsable audio.
		await page.evaluate(() => {
			const dt = new DataTransfer();
			dt.items.add(new File([new Uint8Array(4096).fill(65)], 'e2e-junk.mp3', { type: 'audio/mpeg' }));
			window.dispatchEvent(new DragEvent('dragenter', { dataTransfer: dt, bubbles: true }));
			window.dispatchEvent(new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true }));
		});

		const modal = page.getByTestId('ingest-modal');
		await expect(modal).toBeVisible();
		await expect(modal).toContainText('Ingest 1 file');
		// Config checkbox list came from the real /ingest/config endpoint.
		await expect(modal).toContainText('Analysis (BPM / key / energy)');
		await expect(modal).toContainText('Stems');
		await expect(modal).toContainText('Vocals');

		await page.getByTestId('ingest-run').click();
		await expect(modal).toContainText('Staged to', { timeout: 20_000 });
		await expect(modal).toContainText('e2e-junk.mp3');
	});
});
