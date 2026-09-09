/**
 * Why VocalAnalyzeButton is inert for a row, or null when it can run.
 *
 * Modeled on track-drag-refusal.ts: one pure function so the button's
 * `disabled`/tooltip state is real, executed logic rather than a template
 * expression only readable by eye. `POST /vocals/analyze` mode "from-stems"
 * needs a `ready` local stem bundle (PARITY-08 / issue #1038); every other
 * `StemSummary` state is a refusal.
 */
import type { StemSummary } from './api-rb';

export function vocalsAnalyzeRefusal(stems: StemSummary | null): string | null {
	if (stems === null || stems.status === 'none') {
		return 'vocal analysis needs a stem bundle - see PARITY-TODO';
	}
	if (stems.status === 'invalid') {
		return 'vocal analysis needs a stem bundle - see PARITY-TODO';
	}
	return null;
}
