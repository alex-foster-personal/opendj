import { test, expect } from '@playwright/test';

// E2E for the ingest feature pair (real backend on the claimed worktree
// port, isolated MDT_DATA_DIR - see PR notes):
//  * TopBar refresh-analysis button: visible, hover popover with coverage
//    counts, click starts the refresh job and the popover reaches a
//    terminal phase.
//  * Drag-in ingest modal: an external file drag opens the overlay, a drop
//    opens the modal with the config checkbox list, Stage & run uploads and
//    reports the staged file.

test.describe('refresh analysis button', () => {
	test('hover shows coverage popover, click runs a refresh to done', async ({ page }) => {
		await page.goto('/performance');
		const btn = page.getByTestId('refresh-analysis');
		await expect(btn).toBeVisible();
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

		await btn.click();
		// Empty seeded library: analysis has no targets and vocals from-stems
		// returns quickly, so the job reaches a terminal phase fast.
		await expect(pop).toContainText(/done|error/, { timeout: 25_000 });
		await expect(pop).not.toContainText('error', { timeout: 1_000 });
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
