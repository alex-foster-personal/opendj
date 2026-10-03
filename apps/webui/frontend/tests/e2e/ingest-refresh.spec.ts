import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test, expect } from '@playwright/test';

// Real audio from the locked phase7 corpus: upload refuses a file it cannot
// read a duration from (LIBMX-15), so the drop needs real bytes to stage.
const DROP_FIXTURE = path.resolve(
	fileURLToPath(new URL('.', import.meta.url)),
	'../../../../../tests/fixtures/phase7-dedup/src-128.mp3'
);
// Digest of src-128.mp3 as committed in bf001438d; a changed file fails the
// spec rather than uploading whatever bytes are present.
const DROP_FIXTURE_SHA256 = '922d6cfa0886ef5a6ae195af01d992782d2680bc40244940254934c9aee7c4d0';

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
		// Empty seeded library: analysis has no targets, so the job reaches a
		// terminal phase fast.
		await expect(pop).toContainText(/done|error/, { timeout: 25_000 });
		if (/error/.test(await pop.innerText())) {
			// The popover shows a window of the job log; the status endpoint
			// carries the last LOG_TAIL_LINES. When a pipeline CLI dies with a
			// signal, its faulthandler dump is in there and this is the only
			// place it reaches the CI log (apps.analysis.run exited -11, #1574).
			const status = await page.request.get('/api/v1/ingest/refresh/status');
			const body = (await status.json()) as { log_tail?: string[] };
			console.log(
				['[ingest-refresh] job log at failure:', ...(body.log_tail ?? [])].join('\n')
			);
		}
		await expect(pop).toContainText('done');
		await expect(pop).not.toContainText('error', { timeout: 1_000 });
	});
});

test.describe('ingest drop modal', () => {
	test('file drop opens modal; stage & run uploads and reports', async ({ page }) => {
		await page.goto('/performance');
		// Hydration gate: window drag listeners attach with the component tree.
		await expect(page.getByTestId('refresh-analysis')).toBeVisible();

		// Synthesize an external file drag: DataTransfer with a File holding a
		// real mp3's bytes.
		if (!fs.existsSync(DROP_FIXTURE)) throw new Error(`locked fixture missing: ${DROP_FIXTURE}`);
		const raw = fs.readFileSync(DROP_FIXTURE);
		expect(createHash('sha256').update(raw).digest('hex'), 'src-128.mp3 changed').toBe(
			DROP_FIXTURE_SHA256
		);
		const b64 = raw.toString('base64');
		await page.evaluate((data) => {
			const bytes = Uint8Array.from(atob(data), (c) => c.charCodeAt(0));
			const dt = new DataTransfer();
			dt.items.add(new File([bytes], 'e2e-drop.mp3', { type: 'audio/mpeg' }));
			window.dispatchEvent(new DragEvent('dragenter', { dataTransfer: dt, bubbles: true }));
			window.dispatchEvent(
				new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true })
			);
		}, b64);

		const modal = page.getByTestId('ingest-modal');
		await expect(modal).toBeVisible();
		await expect(modal).toContainText('Ingest 1 file');
		// Config checkbox list came from the real /ingest/config endpoint.
		await expect(modal).toContainText('Analysis (BPM / key / energy)');
		await expect(modal).toContainText('Stems');
		await expect(modal).toContainText('Vocals');

		await page.getByTestId('ingest-run').click();
		await expect(modal).toContainText('Staged to', { timeout: 20_000 });
		await expect(modal).toContainText('e2e-drop.mp3');

		await page.getByRole('button', { name: 'Close' }).click();
		await expect(modal).not.toBeVisible();
		await expect(page.getByTestId('ingest-awaiting-rb')).toBeVisible({ timeout: 10_000 });
	});
});
