/**
 * Shared wire shape for S2 press-to-audible KPI capture.
 *
 * The Playwright driver writes ``KPI_CAPTURE_RESULT``; ``capture_s2_agg.py``
 * scores it. Both sides pin the same row and result fields here so the
 * Python and TypeScript halves cannot drift quietly.
 */

import { PRESS_SCHEDULE_KIND } from '$lib/player/transport/press-audible';
import type { PerfEvent } from '$lib/rb/perf-event-buckets';

/** One transport-schedule-press row the capture may score. */
export type S2PressRow = Pick<PerfEvent, 'kind' | 'deck' | 'stages' | 'labels'>;

/** JSON written to ``KPI_CAPTURE_RESULT`` by ``kpi-s2-capture.spec.ts``. */
export interface KpiS2CaptureResult {
	ok: boolean;
	engine: string;
	browser: string;
	presses: S2PressRow[];
	reason: string | null;
	floor?: Record<string, unknown>;
}

/** Ordinary Class A press rows counted toward the S2 p99. */
export const S2_PRESS_KIND = PRESS_SCHEDULE_KIND;
