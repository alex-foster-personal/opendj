// requirement: PERF-UI-03
import { test } from '@playwright/test';
import { writeFileSync } from 'node:fs';
import { kpiCaptureTimeoutS } from './kpi-capture-timeouts.mjs';

const RESULT_PATH = process.env.KPI_CAPTURE_RESULT;
const CLIENT_EVENTS_PATH = '/api/v1/client-events';
const TIMEOUT_S = kpiCaptureTimeoutS(process.env.KPI_CAPTURE_TIMEOUT_S);

interface CaptureResult {
	ok: boolean;
	span: Record<string, unknown> | null;
	reason: string | null;
}

let resultWritten = false;

function writeResult(result: CaptureResult): void {
	if (!RESULT_PATH) {
		throw new Error('KPI_CAPTURE_RESULT is required');
	}
	writeFileSync(RESULT_PATH, JSON.stringify(result), 'utf-8');
	resultWritten = true;
}

function isBootLibraryPerfSpan(body: Record<string, unknown> | null | undefined): boolean {
	return body?.kind === 'perf-span' && body?.name === 'open-to-library-rows';
}

test('captures open-to-library-rows perf-span on /performance boot', async ({ page }) => {
	test.setTimeout(TIMEOUT_S * 1000 + 30_000);
	const deadline = Date.now() + TIMEOUT_S * 1000;
	let capturedSpan: Record<string, unknown> | null = null;

	page.on('request', (request) => {
		if (request.method() !== 'POST' || !request.url().includes(CLIENT_EVENTS_PATH)) return;
		const body = request.postDataJSON() as Record<string, unknown> | undefined;
		if (isBootLibraryPerfSpan(body ?? null)) {
			capturedSpan = body ?? null;
		}
	});

	await page.addInitScript(() => localStorage.removeItem('odj.brand-launch.v1'));
	await page.goto('/performance', { waitUntil: 'domcontentloaded' });

	while (Date.now() < deadline && capturedSpan === null) {
		await page.waitForTimeout(250);
	}

	if (capturedSpan === null) {
		writeResult({
			ok: false,
			span: null,
			reason: 'missing open-to-library-rows perf-span POST'
		});
		return;
	}

	writeResult({ ok: true, span: capturedSpan, reason: null });
});

test.afterAll(() => {
	if (!RESULT_PATH) return;
	if (!resultWritten) {
		writeResult({
			ok: false,
			span: null,
			reason: 'capture test exited without writing KPI_CAPTURE_RESULT'
		});
	}
});
