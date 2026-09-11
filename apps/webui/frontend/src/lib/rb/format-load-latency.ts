/**
 * Round load latency for the deck corner readout.
 *
 * Latency improvement role: display-only - keeps the number glanceable
 * (2.3s to load / ~300ms to load) so warm vs cold prefetch is obvious
 * without DevTools.
 *
 * The " to load" suffix is part of the readout, not decoration: the deck
 * corner sits next to several other durations, and a bare "~600ms" does not
 * say which one it is (pin 0da471a39765, the maintainer, Wed 2 Sep 2026). The
 * not-measured case stays the empty string so nothing is labelled at all.
 */
const SUFFIX = ' to load';

export function formatLoadLatency(ms: number): string {
	if (!Number.isFinite(ms) || ms < 0) return '';
	if (ms >= 1000) {
		const tenths = Math.round(ms / 100) / 10;
		return `${tenths.toFixed(1)}s${SUFFIX}`;
	}
	const rounded = Math.max(50, Math.round(ms / 50) * 50);
	return `~${rounded}ms${SUFFIX}`;
}
