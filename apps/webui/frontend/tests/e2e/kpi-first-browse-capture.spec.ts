// requirement: PERF-CAPTURE-11113
import { test, type Locator } from '@playwright/test';
import { writeFileSync } from 'node:fs';
import {
	FIRST_BROWSE_POLL_MS,
	firstBrowseTimings,
	initialFirstBrowseState,
	nextFirstBrowseStep
} from './kpi-first-browse-poll.mjs';
import { kpiCaptureTimeoutS } from './kpi-capture-timeouts.mjs';

const RESULT_PATH = process.env.KPI_CAPTURE_RESULT;
const CLIENT_EVENTS_PATH = '/api/v1/client-events';
const TIMEOUT_S = kpiCaptureTimeoutS(process.env.KPI_CAPTURE_TIMEOUT_S);

interface FirstBrowsePayload {
	nav_start_to_click_ms: number | null;
	rows_paint_to_click_ms: number | null;
	open_to_rows_ms: number | null;
	clicks: number;
	rejected_attempts: number;
	span_reason?: string | null;
}

interface CaptureResult {
	ok: boolean;
	span: Record<string, unknown> | null;
	reason: string | null;
	first_browse?: FirstBrowsePayload | null;
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

declare global {
	interface Window {
		__odjFirstBrowseClicks?: number[];
	}
}

test('captures first accepted track-row click on /performance', async ({ page }) => {
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

	await page.addInitScript(() => {
		localStorage.removeItem('odj.brand-launch.v1');
		document.addEventListener(
			'pointerdown',
			(event) => {
				if (!(event.target instanceof Element)) return;
				if (!event.target.closest('tr[data-testid="track-row"]')) return;
				(window.__odjFirstBrowseClicks ??= []).push(event.timeStamp);
			},
			true
		);
	});

	await page.goto('/performance', { waitUntil: 'domcontentloaded' });

	let state = initialFirstBrowseState();
	let targetStableId: string | null = null;
	let targetRow: Locator | null = null;
	let sawVisibleRow = false;

	while (Date.now() < deadline && !state.accepted) {
		if (targetRow === null) {
			const candidate = page.locator('tr[data-testid="track-row"][aria-selected="false"]').first();
			const visible = await candidate.isVisible().catch(() => false);
			if (visible) {
				sawVisibleRow = true;
				targetStableId = (await candidate.getAttribute('data-stable-id')) ?? '__no-stable-id__';
				targetRow = page.locator(`tr[data-testid="track-row"][data-stable-id="${targetStableId}"]`);
			}
		}

		let rowVisible = false;
		let ariaSelected: 'true' | 'false' | null = null;
		if (targetRow !== null) {
			rowVisible = await targetRow.isVisible().catch(() => false);
			if (rowVisible) {
				const raw = await targetRow.getAttribute('aria-selected');
				if (raw === 'true' || raw === 'false') {
					ariaSelected = raw;
				}
			}
		}

		const step = nextFirstBrowseStep(state, { rowVisible, ariaSelected });
		state = step.state;
		if (step.action === 'click' && targetRow !== null) {
			try {
				await targetRow.click({ timeout: 1_000, trial: false });
			} catch {
				// Detached or covered: retry next tick without counting acceptance.
			}
		} else if (step.action === 'accepted') {
			break;
		}
		await page.waitForTimeout(FIRST_BROWSE_POLL_MS);
	}

	if (!sawVisibleRow) {
		writeResult({
			ok: false,
			span: capturedSpan,
			reason: `timeout: no visible track row within ${TIMEOUT_S}s`,
			first_browse: {
				nav_start_to_click_ms: null,
				rows_paint_to_click_ms: null,
				open_to_rows_ms: null,
				clicks: state.clicks,
				rejected_attempts: state.rejected
			}
		});
		return;
	}

	if (!state.accepted) {
		writeResult({
			ok: false,
			span: capturedSpan,
			reason: `timeout: no accepted row click within ${TIMEOUT_S}s (rejected_attempts=${state.rejected})`,
			first_browse: {
				nav_start_to_click_ms: null,
				rows_paint_to_click_ms: null,
				open_to_rows_ms: null,
				clicks: state.clicks,
				rejected_attempts: state.rejected
			}
		});
		return;
	}

	const spanDeadline = Date.now() + 2_000;
	while (Date.now() < spanDeadline && capturedSpan === null) {
		await page.waitForTimeout(100);
	}

	const clickTimestampsMs = await page.evaluate(() => window.__odjFirstBrowseClicks ?? []);
	if (!Array.isArray(clickTimestampsMs) || clickTimestampsMs.length === 0) {
		writeResult({
			ok: false,
			span: capturedSpan,
			reason: 'missing-telemetry: accepted selection without a recorded pointerdown',
			first_browse: {
				nav_start_to_click_ms: null,
				rows_paint_to_click_ms: null,
				open_to_rows_ms: null,
				clicks: state.clicks,
				rejected_attempts: state.rejected
			}
		});
		return;
	}

	// The span is assigned inside the route handler, so the compiler narrows the variable to null here.
	const settledSpan = capturedSpan as Record<string, unknown> | null;
	const openToRowsMs =
		settledSpan !== null &&
		typeof settledSpan.duration_ms === 'number' &&
		Number.isFinite(settledSpan.duration_ms)
			? Number(settledSpan.duration_ms)
			: null;

	const { nav_start_to_click_ms, rows_paint_to_click_ms } = firstBrowseTimings({
		clickTimestampsMs,
		openToRowsMs
	});

	let span_reason: string | null = null;
	if (rows_paint_to_click_ms === null) {
		if (openToRowsMs === null) {
			span_reason = 'missing open-to-library-rows perf-span';
		} else if (nav_start_to_click_ms !== null && openToRowsMs > nav_start_to_click_ms) {
			span_reason = 'clock skew: click before first row paint';
		}
	}

	writeResult({
		ok: true,
		span: capturedSpan,
		reason: null,
		first_browse: {
			nav_start_to_click_ms,
			rows_paint_to_click_ms,
			open_to_rows_ms: openToRowsMs,
			clicks: state.clicks,
			rejected_attempts: state.rejected,
			span_reason
		}
	});
});

test.afterAll(() => {
	if (!RESULT_PATH) return;
	if (!resultWritten) {
		writeResult({
			ok: false,
			span: null,
			reason: 'capture test exited without writing KPI_CAPTURE_RESULT',
			first_browse: null
		});
	}
});
