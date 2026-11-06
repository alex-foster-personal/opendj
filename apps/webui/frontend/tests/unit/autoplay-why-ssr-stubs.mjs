/** Stubs so AutoPlayWhy SSR does not pull performance-ipc / live API. */
export const VERDICTS = ['good', 'meh', 'bad'];
export function chosenCandidate() {
	return null;
}
export async function fetchLastDecision() {
	return null;
}
export function verdictOffered() {
	return false;
}
export async function runPerformanceCommandFromUi() {
	return { ok: true };
}
export const autoPlayDecision = { phase: 'idle', track: null };
