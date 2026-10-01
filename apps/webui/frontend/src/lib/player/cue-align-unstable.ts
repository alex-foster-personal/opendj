/**
 * Why a cue-alignment run was refused as unstable (pin 9a722fd1).
 *
 * The three paired runs disagreeing by more than the allowed spread used to
 * end in one fixed sentence, "try again with less room noise", whatever was
 * measured. That sent an operator in a silent room hunting for noise. Every
 * run already carries what is needed to say what really happened, so the
 * reason is read off the measurements, in this order:
 *
 * 1. `input_clipped`      the microphone saturated on a capture. A clipped
 *                         chirp correlates with a smeared peak, so its lag is
 *                         not trustworthy however loud it scored.
 * 2. `signal_too_quiet`   a chirp cleared the accept threshold but not the
 *                         comfortable one, so its lag rides on a weak peak.
 * 3. `path_latency_moved` every chirp was heard clearly and unclipped, and the
 *                         lag still moved. That is the output path changing
 *                         its own buffering, not anything acoustic.
 *
 * A chirp that was not heard at all never reaches this module: the level
 * checks refuse it earlier as CueAlignMicCouldNotHear.
 */

/** A sample at or above this magnitude is at full scale. */
export const CLIPPED_SAMPLE_LEVEL = 0.99;
/** A capture is clipped when MORE than this fraction of it is at full scale.
 * One stray full-scale sample in several seconds is a click, not saturation. */
export const CLIPPED_FRACTION_MAX = 0.001;

export type UnstableReason = 'input_clipped' | 'signal_too_quiet' | 'path_latency_moved';
export type UnstableBus = 'master' | 'cue' | 'both';

/** What one measured chirp reported. */
export interface MeasuredRun {
	lagMs: number;
	/** Normalized correlation peak, 0..1. */
	peak: number;
	/** Fraction of the capture at full scale, 0..1. */
	clippedFraction: number;
}

export interface UnstableLimits {
	/** Peak at which a chirp counts as comfortably heard. */
	comfortablePeak: number;
	/** Largest lag spread one bus may show before it counts as having moved. */
	maxSpreadMs: number;
}

export interface UnstableDiagnosis {
	reason: UnstableReason;
	bus: UnstableBus;
	message: string;
}

const BUS_LABEL = { master: 'speakers', cue: 'headphones' } as const;

export function clippedFraction(samples: Float32Array): number {
	if (samples.length === 0) throw new RangeError('clippedFraction needs a non-empty capture');
	let clipped = 0;
	for (let i = 0; i < samples.length; i += 1) {
		if (Math.abs(samples[i]) >= CLIPPED_SAMPLE_LEVEL) clipped += 1;
	}
	return clipped / samples.length;
}

function _spread(runs: readonly MeasuredRun[]): number {
	const lags = runs.map((run) => run.lagMs);
	return Math.max(...lags) - Math.min(...lags);
}

function _worst(
	master: readonly MeasuredRun[],
	cue: readonly MeasuredRun[],
	pick: (run: MeasuredRun) => number,
	better: (left: number, right: number) => boolean
): { bus: 'master' | 'cue'; value: number } {
	let worst: { bus: 'master' | 'cue'; value: number } | null = null;
	for (const [bus, runs] of [['master', master], ['cue', cue]] as const) {
		for (const run of runs) {
			const value = pick(run);
			if (worst === null || better(value, worst.value)) worst = { bus, value };
		}
	}
	if (worst === null) throw new RangeError('an unstable diagnosis needs at least one measured run');
	return worst;
}

export function diagnoseUnstableMeasurement(
	master: readonly MeasuredRun[],
	cue: readonly MeasuredRun[],
	limits: UnstableLimits
): UnstableDiagnosis {
	const mostClipped = _worst(master, cue, (run) => run.clippedFraction, (a, b) => a > b);
	if (mostClipped.value > CLIPPED_FRACTION_MAX) {
		return {
			reason: 'input_clipped',
			bus: mostClipped.bus,
			message:
				`the microphone clipped while recording the ${BUS_LABEL[mostClipped.bus]} ` +
				`(${(mostClipped.value * 100).toFixed(1)}% of samples at full scale), so that timing ` +
				`cannot be trusted. Turn that output down, or lower the microphone input level, and run it again.`
		};
	}
	const weakest = _worst(master, cue, (run) => run.peak, (a, b) => a < b);
	if (weakest.value < limits.comfortablePeak) {
		const fix =
			weakest.bus === 'master'
				? 'Turn the speakers up or move the microphone closer to them.'
				: 'Hold the ear cup against the microphone and turn that output up.';
		return {
			reason: 'signal_too_quiet',
			bus: weakest.bus,
			message:
				`the ${BUS_LABEL[weakest.bus]} chirp was only just heard ` +
				`(weakest peak ${weakest.value.toFixed(2)}, comfortable is ${limits.comfortablePeak}), ` +
				`so its timing is not reliable. ${fix}`
		};
	}
	const masterMoved = _spread(master) > limits.maxSpreadMs;
	const cueMoved = _spread(cue) > limits.maxSpreadMs;
	// Neither bus alone exceeding the limit still fails the run when they
	// drift in opposite directions, and then no single bus is to blame.
	const bus: UnstableBus = masterMoved === cueMoved ? 'both' : masterMoved ? 'master' : 'cue';
	const who = bus === 'both' ? 'both outputs changed their latency' : `the ${BUS_LABEL[bus]} latency itself changed`;
	return {
		reason: 'path_latency_moved',
		bus,
		message:
			`${who} between runs. Every chirp was heard clearly ` +
			`(weakest peak ${weakest.value.toFixed(2)}) and none clipped, so this is the output device ` +
			`changing its own buffering, which a wireless link does, and not a noisy room. Run it again; ` +
			`if it keeps moving, that device cannot hold a fixed alignment.`
	};
}

/** The three paired runs disagreed by more than the allowed spread. */
export class CueAlignMeasurementUnstable extends Error {
	readonly reason: UnstableReason;
	readonly bus: UnstableBus;
	constructor(
		spreadMs: number,
		master: readonly MeasuredRun[],
		cue: readonly MeasuredRun[],
		limits: UnstableLimits
	) {
		const diagnosis = diagnoseUnstableMeasurement(master, cue, limits);
		const lags = (runs: readonly MeasuredRun[]): string => runs.map((run) => run.lagMs).join(', ');
		super(
			`measurement unstable (spread ${spreadMs} ms): ${diagnosis.message} ` +
				`(speakers ${lags(master)} ms; headphones ${lags(cue)} ms)`
		);
		this.name = 'CueAlignMeasurementUnstable';
		this.reason = diagnosis.reason;
		this.bus = diagnosis.bus;
	}
}
