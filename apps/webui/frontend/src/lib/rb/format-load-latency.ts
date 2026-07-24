/**
 * Round load latency for the deck corner readout.
 *
 * Latency improvement role: display-only - keeps the number glanceable
 * (2.3s / ~300ms) so warm vs cold prefetch is obvious without DevTools.
 */
export function formatLoadLatency(ms: number): string {
	if (!Number.isFinite(ms) || ms < 0) return '';
	if (ms >= 1000) {
		const tenths = Math.round(ms / 100) / 10;
		return `${tenths.toFixed(1)}s`;
	}
	const rounded = Math.max(50, Math.round(ms / 50) * 50);
	return `~${rounded}ms`;
}
