/**
 * Temporary UI-state countdown (e.g. genre filter gesture window).
 * start / cancel / remaining - cheap; UI ticks separately.
 */

export class OttTimer {
	/** Epoch ms when the window ends; 0 = inactive. */
	until = $state(0);
	/** Original duration (ms) for progress ring math. */
	total = $state(0);

	start(durationMs: number): void {
		if (!Number.isFinite(durationMs) || durationMs <= 0) {
			throw new Error(`OttTimer.start: durationMs must be positive, got ${durationMs}`);
		}
		this.total = durationMs;
		this.until = Date.now() + durationMs;
	}

	/** Resume / restore a window ending at an absolute epoch. */
	startUntil(untilMs: number, totalMs: number): void {
		if (!Number.isFinite(untilMs) || untilMs <= 0) {
			this.cancel();
			return;
		}
		if (!Number.isFinite(totalMs) || totalMs <= 0) {
			throw new Error(`OttTimer.startUntil: totalMs must be positive, got ${totalMs}`);
		}
		this.total = totalMs;
		this.until = untilMs;
	}

	cancel(): void {
		this.until = 0;
		this.total = 0;
	}

	remaining(now: number = Date.now()): number {
		if (this.until <= 0) return 0;
		return Math.max(0, this.until - now);
	}

	/** 1 = full time left, 0 = expired. */
	fraction(now: number = Date.now()): number {
		if (this.total <= 0 || this.until <= 0) return 0;
		return Math.min(1, this.remaining(now) / this.total);
	}

	get active(): boolean {
		return this.remaining() > 0;
	}
}

export function createOttTimer(): OttTimer {
	return new OttTimer();
}
