/**
 * Jog wheel behaviour for controllers without a scratch engine.
 *
 * Paused the wheel scrubs; playing it bends the tempo. A wheel sends hundreds
 * of ticks a second, so ticks are summed and applied every JOG_FLUSH_MS: one
 * command per tick would queue behind the engine and lag the hand. The bend
 * therefore follows wheel speed. Beat Sync owns a follower deck's tempo, so a
 * bend there is refused once per gesture.
 *
 * Tick size and bend strength are estimates: Mixxx FLX10 mappings use 1500 and
 * 5760 ticks a turn, so they are set from a real wheel, not derived.
 */

const JOG_SCRUB_MS_PER_TICK = 2.5;
const JOG_BEND_PER_TICK = 0.002;
/** Caps a hard spin at a nudge. */
const JOG_BEND_MAX = 0.04;
export const JOG_FLUSH_MS = 25;
/** The wheel has stopped once no tick has arrived for this long. */
export const JOG_RELEASE_MS = 80;
/** Seeks can queue, so a scrub builds on its last target until the wheel rests. */
const JOG_SCRUB_SETTLE_MS = 250;
/** A deck this far from the last target was moved by something else. */
const JOG_SCRUB_RESYNC_MS = 1000;

const _clamp = (value: number, low: number, high: number): number =>
	Math.min(Math.max(value, low), high);

export function jogScrubTargetMs(positionMs: number, durationMs: number, delta: number): number {
	return _clamp(positionMs + delta * JOG_SCRUB_MS_PER_TICK, 0, durationMs);
}

export function jogBendRatio(baseRatio: number, delta: number, rangePct: number): number {
	const bend = _clamp(delta * JOG_BEND_PER_TICK, -JOG_BEND_MAX, JOG_BEND_MAX);
	const range = rangePct / 100;
	return _clamp(baseRatio + bend, 1 - range, 1 + range);
}

export interface JogDeckView {
	playing: boolean;
	followsSyncMaster: boolean;
	pitch: number;
	rangePct: number;
	positionMs: number;
	durationMs: number;
}

export interface JogSink {
	seek(positionMs: number): void;
	tempo(ratio: number): void;
	bendRefused(): void;
}

export class JogWheel {
	private _pendingTicks = 0;
	private _flushTimer: ReturnType<typeof setTimeout> | null = null;
	private _releaseTimer: ReturnType<typeof setTimeout> | null = null;
	private _settleTimer: ReturnType<typeof setTimeout> | null = null;
	private _bendBaseRatio: number | null = null;
	private _scrubTargetMs: number | null = null;
	private _refusedThisGesture = false;

	constructor(
		private readonly _read: () => JogDeckView,
		private readonly _sink: JogSink
	) {}

	turn(delta: number): void {
		this._pendingTicks += delta;
		this._flushTimer ??= setTimeout(() => this._flush(), JOG_FLUSH_MS);
		if (this._releaseTimer !== null) clearTimeout(this._releaseTimer);
		this._releaseTimer = setTimeout(() => this._release(), JOG_RELEASE_MS);
	}

	/** A TEMPO move during a bend becomes the tempo the wheel releases to. */
	tempoMoved(ratio: number): void {
		if (this._bendBaseRatio !== null) this._bendBaseRatio = ratio;
	}

	private _flush(): void {
		this._flushTimer = null;
		const ticks = this._pendingTicks;
		this._pendingTicks = 0;
		const deck = this._read();
		if (!deck.playing) this._scrub(deck, ticks);
		else if (deck.followsSyncMaster) this._refuseBend();
		else this._bend(deck, ticks);
	}

	private _scrub(deck: JogDeckView, ticks: number): void {
		const pending = this._scrubTargetMs;
		const stale = pending === null || Math.abs(pending - deck.positionMs) > JOG_SCRUB_RESYNC_MS;
		const target = jogScrubTargetMs(stale ? deck.positionMs : pending, deck.durationMs, ticks);
		this._scrubTargetMs = target;
		if (this._settleTimer !== null) clearTimeout(this._settleTimer);
		this._settleTimer = setTimeout(() => {
			this._scrubTargetMs = null;
		}, JOG_SCRUB_SETTLE_MS);
		this._sink.seek(target);
	}

	private _bend(deck: JogDeckView, ticks: number): void {
		this._bendBaseRatio ??= deck.pitch;
		this._sink.tempo(jogBendRatio(this._bendBaseRatio, ticks, deck.rangePct));
	}

	private _refuseBend(): void {
		if (this._refusedThisGesture) return;
		this._refusedThisGesture = true;
		this._sink.bendRefused();
	}

	private _release(): void {
		this._releaseTimer = null;
		this._refusedThisGesture = false;
		if (this._flushTimer !== null) {
			clearTimeout(this._flushTimer);
			this._flush();
			this._refusedThisGesture = false;
		}
		if (this._bendBaseRatio === null) return;
		this._sink.tempo(this._bendBaseRatio);
		this._bendBaseRatio = null;
	}
}
