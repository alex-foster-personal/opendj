import { mkdirSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { expect, test, type APIRequestContext, type Locator, type Page } from '@playwright/test';

import { waitForPerformanceIpc } from './support/performance-ready';
import {
	renderedContrastAudit,
	trackCanvasText,
	type ContrastFinding
} from './support/rendered-contrast';
import { applySkin, restoreThemeAfterEach, SKINS, type Skin } from './support/skin-prefs';

// requirement: UICONTRAST-6037 (issue #5994, follow-up to UICONTRAST-01 #5809)
// [if] running-job text is below WCAG AA in a skin [then] the run fails, [else stop].
// Every pass audits a REAL stems.separate job the engine is running: two
// tracks, the first already separated, so the job holds at a published 50%
// while local Demucs works on the second. The percentage label then straddles
// the fill edge, and the rendered-contrast instrument samples both ends of it.

const STEMS_DIR = fileURLToPath(
	new URL('./fixtures/root-playwright-data/state/stems/', import.meta.url)
);
const SHOT_DIR = fileURLToPath(new URL('../../test-results/jobs-contrast/', import.meta.url));
const JOB_KIND = 'stems.separate';
const SEPARATE_TIMEOUT_MS = 300_000;
const PLANTED = 'planted-bad-pair';

/** Surfaces audited per skin: the selector scoped, and the fewest text elements it must measure. */
const SURFACES = [
	{ name: 'StemsProgress', scope: 'button.stems-progress', floor: 2 },
	{ name: 'JobsDrawer', scope: 'aside.jobs-panel', floor: 8 }
] as const;

interface JobRow {
	id: string;
	status: string;
	progress: number;
}

test.beforeEach(async ({ page }) => {
	await page.addInitScript(trackCanvasText);
});
restoreThemeAfterEach();

//----- engine jobs

async function _enqueue(request: APIRequestContext, stableIds: string[]): Promise<string> {
	const res = await request.post('/api/v1/jobs', {
		data: { kind: JOB_KIND, payload: { stable_ids: stableIds } }
	});
	expect(res.ok(), `POST /api/v1/jobs: ${res.status()} ${await res.text()}`).toBe(true);
	return ((await res.json()) as JobRow).id;
}

async function _job(request: APIRequestContext, id: string): Promise<JobRow> {
	const res = await request.get(`/api/v1/jobs/${id}`);
	expect(res.ok(), `GET /api/v1/jobs/${id}: ${res.status()}`).toBe(true);
	return (await res.json()) as JobRow;
}

async function _waitJob(
	request: APIRequestContext,
	id: string,
	status: string,
	progress: number
): Promise<void> {
	await expect
		.poll(
			async () => {
				const job = await _job(request, id);
				if (job.status === 'failed' || job.status === 'unknown')
					throw new Error(`job ${id} ended ${job.status}`);
				return `${job.status} ${job.progress}`;
			},
			{ timeout: SEPARATE_TIMEOUT_MS, intervals: [500] }
		)
		.toBe(`${status} ${progress}`);
}

//----- audit

/** Every measured row in `scope` (each is marked a probe), with one bad pair planted as the control. */
async function _auditScope(
	page: Page,
	scope: string
): Promise<{ rows: ContrastFinding[]; findings: ContrastFinding[] }> {
	await page.evaluate(
		([sel, text]) => {
			const root = document.querySelector(sel)!;
			root.setAttribute('data-contrast-probe', '');
			const el = document.createElement('span');
			el.id = 'contrast-planted';
			el.textContent = text;
			el.style.cssText =
				'position:absolute;left:0;top:0;background:#888888;color:#777777;font-size:12px';
			root.appendChild(el);
		},
		[scope, PLANTED] as const
	);
	const report = await page.evaluate(renderedContrastAudit, scope);
	await page.evaluate(() => document.getElementById('contrast-planted')?.remove());
	expect(report.unmeasured, `${scope}: text the audit could not measure`).toEqual([]);
	expect(
		report.findings.filter((f) => f.text === PLANTED).map((f) => f.ratio),
		`${scope}: planted pair`
	).toEqual([1.26]);
	const rows = report.probes.filter((f) => f.text !== PLANTED);
	return { rows, findings: report.findings.filter((f) => f.text !== PLANTED) };
}

async function _shot(target: Locator, skin: Skin, name: string): Promise<void> {
	mkdirSync(SHOT_DIR, { recursive: true });
	await target.screenshot({ path: join(SHOT_DIR, `${skin}-${name}.png`) });
}

test('running-job progress text clears WCAG AA in every skin', async ({ page, request }) => {
	test.setTimeout(SEPARATE_TIMEOUT_MS * (SKINS.length + 1));
	await page.setViewportSize({ width: 1600, height: 900 });
	const tracks = await (await request.get('/api/v1/tracks?limit=2')).json();
	const [primer, fresh]: string[] = tracks.items.map((t: { stable_id: string }) => t.stable_id);
	expect(fresh, 'the fixture library has two tracks').toBeTruthy();
	await _waitJob(request, await _enqueue(request, [primer]), 'succeeded', 1);

	const table: string[] = [];
	const failing: string[] = [];
	for (const skin of SKINS) {
		await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
		await waitForPerformanceIpc(page);
		await applySkin(page, skin);
		rmSync(join(STEMS_DIR, fresh), { recursive: true, force: true });
		const id = await _enqueue(request, [primer, fresh]);
		await _waitJob(request, id, 'running', 0.5);

		const bar = page.locator('button.stems-progress');
		await expect(bar.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '50');
		const audits = [await _auditScope(page, SURFACES[0].scope)];
		await _shot(bar, skin, SURFACES[0].name);
		await bar.click();
		const panel = page.locator(SURFACES[1].scope);
		await expect(panel.locator('.jobs-chip.st-running')).toBeVisible();
		await expect(
			panel.getByRole('progressbar', { name: `${JOB_KIND} progress` }).first()
		).toHaveAttribute('aria-valuenow', '50');
		audits.push(await _auditScope(page, SURFACES[1].scope));
		await _shot(panel, skin, SURFACES[1].name);

		for (const [i, { rows, findings }] of audits.entries()) {
			const { name, floor } = SURFACES[i];
			expect(
				rows.length,
				`${skin}/${name} measured fewer elements than its floor`
			).toBeGreaterThanOrEqual(floor);
			for (const r of rows) {
				const verdict = r.ratio + 1e-9 < r.need ? 'FAIL' : 'pass';
				table.push(
					`| ${skin} | ${name} | \`${r.el}\` | ${r.text} | ${r.fg} on ${r.bg} | ${r.ratio}:1 | ${r.need}:1 | ${verdict} |`
				);
			}
			failing.push(
				...findings.map(
					(f) =>
						`${skin} | ${name} | ${f.el} "${f.text}" ${f.fg} on ${f.bg} = ${f.ratio}:1 (needs ${f.need}:1)`
				)
			);
		}
		await page.locator('button.jobs-x').click();
		await _waitJob(request, id, 'succeeded', 1);
	}

	console.log(
		`[UICONTRAST-6037] running-job contrast:\n| skin | surface | element | text | fg on bg | ratio | needs | verdict |\n|---|---|---|---|---|---|---|---|\n${table.join('\n')}`
	);
	expect(failing, 'running-job text below WCAG AA').toEqual([]);
});
