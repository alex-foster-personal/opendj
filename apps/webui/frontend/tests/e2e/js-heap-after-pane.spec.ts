// requirement: PERF-CAPTURE-10797
//
// Measures main-isolate JS heap (Chromium CDP) after the /performance boot All Tracks
// pane fills (perf ring library-load-all-tracks). Chromium only: WebKit and Firefox lack
// HeapProfiler.collectGarbage and Runtime.getHeapUsage on the page target.
// Operator-only; driven by scripts/perf/js_heap_after_pane.py.

import { test, type CDPSession } from '@playwright/test';
import { writeFileSync } from 'node:fs';
import { kpiCaptureTimeoutS } from './kpi-capture-timeouts.mjs';

const RESULT_PATH = process.env.KPI_CAPTURE_RESULT;
const TIMEOUT_S = kpiCaptureTimeoutS(process.env.KPI_CAPTURE_TIMEOUT_S);
const RING_KIND = 'library-load-all-tracks';
const IDLE_GAP_MS = 2_000;
const POLL_MS = 250;

interface HeapResult {
	ok: boolean;
	reason: string | null;
	rows: number | null;
	after_pane_used_bytes: number | null;
	idle_used_bytes: number | null;
	ring_row: Record<string, unknown> | null;
}

let resultWritten = false;

function writeResult(result: HeapResult): void {
	if (!RESULT_PATH) {
		throw new Error('KPI_CAPTURE_RESULT is required');
	}
	writeFileSync(RESULT_PATH, JSON.stringify(result), 'utf-8');
	resultWritten = true;
}

function failed(reason: string, partial: Partial<HeapResult> = {}): void {
	writeResult({
		ok: false,
		reason,
		rows: partial.rows ?? null,
		after_pane_used_bytes: null,
		idle_used_bytes: null,
		ring_row: partial.ring_row ?? null
	});
}

type PerfRingWindow = Window & {
	__mdtPerfLog?: () => readonly {
		kind?: string;
		stages?: Record<string, unknown>;
	}[];
};

async function settledUsedBytes(cdp: CDPSession): Promise<number | null> {
	for (let i = 0; i < 2; i += 1) {
		await cdp.send('HeapProfiler.collectGarbage');
	}
	const usage = (await cdp.send('Runtime.getHeapUsage')) as { usedSize?: number };
	const used = usage.usedSize;
	if (typeof used !== 'number' || !Number.isFinite(used) || used <= 0) {
		return null;
	}
	return used;
}

test('reads the JS heap after the All Tracks pane fills on /performance boot', async ({
	page,
	browserName
}) => {
	test.skip(
		browserName !== 'chromium',
		'HeapProfiler.collectGarbage and Runtime.getHeapUsage require Chromium CDP'
	);
	test.setTimeout(TIMEOUT_S * 1000 + 30_000);

	const deadline = Date.now() + TIMEOUT_S * 1000;
	await page.addInitScript(() => localStorage.removeItem('odj.brand-launch.v1'));
	await page.goto('/performance', { waitUntil: 'domcontentloaded' });

	let ringRow: Record<string, unknown> | null = null;
	while (Date.now() < deadline && ringRow === null) {
		try {
			ringRow = await page.evaluate((kind) => {
				const log = (window as PerfRingWindow).__mdtPerfLog?.() ?? [];
				for (const row of log) {
					if (row?.kind === kind) {
						return row as Record<string, unknown>;
					}
				}
				return null;
			}, RING_KIND);
		} catch {
			ringRow = null;
		}
		if (ringRow === null) {
			await page.waitForTimeout(POLL_MS);
		}
	}

	if (ringRow === null) {
		failed(`perf ring never reported ${RING_KIND} within ${TIMEOUT_S}s`);
		return;
	}

	const stages = ringRow.stages as Record<string, unknown> | undefined;
	const rawRows = stages?.rows;
	const rows =
		typeof rawRows === 'number' &&
		Number.isFinite(rawRows) &&
		rawRows > 0 &&
		Number.isInteger(rawRows)
			? rawRows
			: null;
	if (rows === null) {
		failed('library-load-all-tracks row carried no positive stages.rows', { ring_row: ringRow });
		return;
	}

	try {
		const cdp = await page.context().newCDPSession(page);
		const afterPane = await settledUsedBytes(cdp);
		await page.waitForTimeout(IDLE_GAP_MS);
		const idle = await settledUsedBytes(cdp);
		if (afterPane === null || idle === null) {
			failed('Runtime.getHeapUsage returned no positive usedSize after forced GC', {
				rows,
				ring_row: ringRow
			});
			return;
		}
		writeResult({
			ok: true,
			reason: null,
			rows,
			after_pane_used_bytes: afterPane,
			idle_used_bytes: idle,
			ring_row: ringRow
		});
	} catch (err) {
		const message = err instanceof Error ? err.message : String(err);
		failed(`CDP heap read failed: ${message}`, { rows, ring_row: ringRow });
	}
});

test.afterAll(() => {
	if (!RESULT_PATH) return;
	if (!resultWritten) {
		writeResult({
			ok: false,
			reason: 'heap capture test exited without writing KPI_CAPTURE_RESULT',
			rows: null,
			after_pane_used_bytes: null,
			idle_used_bytes: null,
			ring_row: null
		});
	}
});
