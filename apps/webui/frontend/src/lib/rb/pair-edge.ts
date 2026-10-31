/**
 * The pair "lightning edge", variant G Comet (the maintainer's pick, Thu 8 Oct 2026):
 * a jagged ring drawn just inside a paired row, with one bright pulse that
 * runs the whole way round once, fading in and back out along the lap with a
 * short trail behind it, then stays quiet.
 *
 * One place for the numbers and the geometry, shared by every paired row in
 * the app: the Cmd K bar's pairs (CMDK-18, issue #6092) and the Pairings
 * library (#6062) draw the same edge. The values are `ZAPS.comet` from the
 * design prototype ((private path)); its color is the
 * `--rb-pair` token in theme.css. Pure and runeless, so the unit suite loads it.
 */

/** `ZAPS.comet`, verbatim from the prototype. */
export const COMET = Object.freeze({
	/** Jag amplitude, px. */
	amp: 0.4,
	/** How much a slow travelling wave swells the jags (0 = none). */
	swell: 0.3,
	/** Swell waves round the ring. */
	waves: 2,
	/** Swell wave speed, radians per second. */
	wspeed: 3,
	/** One lap of the pulse, seconds. */
	lap: 0.85,
	/** The pulse's length as a fraction of the ring. */
	blen: 0.07,
	/** How often the jags re-roll, per second. */
	fps: 16,
	/** Opacity of the quiet ring. */
	base: 0.4,
	/** Peak opacity of the pulse head. */
	bright: 0.95
});

/** The ring sits this far inside the row, so stacked rows never overlap. */
export const RING_INSET = 2.5;
/** One jag every this many px of perimeter (at least MIN_POINTS). */
const JAG_SPACING = 5;
const MIN_POINTS = 24;
/** Trail paths behind the head; each lags the one before by TRAIL_LAG of a lap. */
export const TRAIL_PATHS = 4;
const TRAIL_LAG = 0.035;
/** The pulse is gone once the last trail path has finished its lap. */
export const LAP_END = 1 + TRAIL_PATHS * TRAIL_LAG;

export interface Ring {
	/** SVG path data, closed. */
	d: string;
	/** Perimeter of the inset rectangle, px: the dash maths' 100%. */
	per: number;
}

/**
 * The jagged ring for a `w` x `h` row at time `t` seconds. `seed` keeps two
 * rows from jagging in step. Offsets are clamped inside the inset, so the
 * ring never leaves its row.
 */
export function ringPath(w: number, h: number, t: number, seed: number): Ring {
	const inset = RING_INSET;
	const W = Math.max(0, w - 2 * inset);
	const H = Math.max(0, h - 2 * inset);
	const per = 2 * (W + H);
	const n = Math.max(MIN_POINTS, Math.round(per / JAG_SPACING));
	const tick = Math.floor(t * COMET.fps);
	const limit = inset - 0.6;
	const pts: string[] = [];
	for (let i = 0; i < n; i++) {
		const d = (i / n) * per;
		let x: number;
		let y: number;
		let nx: number;
		let ny: number;
		if (d < W) {
			x = d;
			y = 0;
			nx = 0;
			ny = -1;
		} else if (d < W + H) {
			x = W;
			y = d - W;
			nx = 1;
			ny = 0;
		} else if (d < 2 * W + H) {
			x = W - (d - W - H);
			y = H;
			nx = 0;
			ny = 1;
		} else {
			x = 0;
			y = H - (d - 2 * W - H);
			nx = -1;
			ny = 0;
		}
		const jag = (i % 2 ? 1 : -1) * COMET.amp * (0.55 + 0.45 * Math.abs(Math.sin(i * 12.9898 + seed + tick * 3.1)));
		const swell = 1 + COMET.swell * Math.sin((d / (per || 1)) * Math.PI * 2 * COMET.waves - t * COMET.wspeed);
		const off = Math.max(-limit, Math.min(limit, jag * swell));
		pts.push(`${(inset + x + nx * off).toFixed(1)},${(inset + y + ny * off).toFixed(1)}`);
	}
	return { d: `M${pts.join('L')}Z`, per };
}

export interface PulseStroke {
	dasharray: string;
	dashoffset: string;
	opacity: number;
}

/**
 * The head and trail strokes `elapsed` seconds into a lap: a dash of
 * `blen` of the ring that runs once round, its opacity following sin(pi p)
 * so it fades in and back out, each trail path lagging and dimmer. Every
 * stroke is invisible before the lap and once it has ended (`LAP_END`).
 */
export function pulseStrokes(elapsed: number, per: number): PulseStroke[] {
	const p = elapsed / COMET.lap;
	const seg = per * COMET.blen;
	const count = TRAIL_PATHS + 1;
	const out: PulseStroke[] = [];
	for (let k = 0; k < count; k++) {
		if (p < 0 || p > LAP_END) {
			out.push({ dasharray: `${seg} ${per - seg}`, dashoffset: '0', opacity: 0 });
			continue;
		}
		const pk = Math.min(1, Math.max(0, p - k * TRAIL_LAG));
		const env = Math.sin(Math.PI * pk);
		out.push({
			dasharray: `${seg} ${per - seg}`,
			dashoffset: String(-(pk * per)),
			opacity: Math.max(0, COMET.bright * env * (1 - k / (count + 0.5)))
		});
	}
	return out;
}

/** True while a lap started at `start` (seconds) is still drawing at `now`. */
export function lapRunning(start: number | null, now: number): boolean {
	return start !== null && now >= start && (now - start) / COMET.lap <= LAP_END;
}

/**
 * Whether a hover may restart the pulse: never mid-lap, so sweeping the
 * pointer across a row does not stutter it (the prototype's `replayComet`).
 */
export function mayReplay(start: number | null, now: number): boolean {
	// The trailing strokes run to LAP_END, past the head's own lap.
	return start === null || now - start > COMET.lap * LAP_END;
}
