/**
 * Audio I/O device check: three short beeps on an output, plus the 0..1
 * fill for the thin vertical meters beside the device dropdowns.
 *
 * Master and input beeps go to the engine's main destination (the pinned
 * room output). Cue beeps go to the headphone bus when that graph is up.
 * An input cannot play, so its button uses the master path; the input
 * meter is the microphone.
 */

export type IoTestBus = 'master' | 'cue' | 'input';

/** Three short beeps. Same pitch so it reads as a test, not a jingle. */
export const IO_TEST_BEEPS: readonly { hz: number; holdSec: number; gapSec: number }[] = [
	{ hz: 880, holdSec: 0.08, gapSec: 0.07 },
	{ hz: 880, holdSec: 0.08, gapSec: 0.07 },
	{ hz: 880, holdSec: 0.12, gapSec: 0 }
];

/** Sine level. Loud enough to hear on a quiet bus, short enough to talk over. */
export const IO_TEST_GAIN = 0.18;

/** Meter height while a beep is sounding. Under the red threshold. */
export const IO_TEST_METER_FILL = 0.78;

const METER_FLOOR_DB = -48;

export function ioTestDurationSec(): number {
	return IO_TEST_BEEPS.reduce((total, beep) => total + beep.holdSec + beep.gapSec, 0);
}

/** 0 outside a beep, `IO_TEST_METER_FILL` while one is sounding. */
export function ioTestEnvelope(elapsedSec: number): number {
	if (!Number.isFinite(elapsedSec) || elapsedSec < 0) return 0;
	let t = 0;
	for (const beep of IO_TEST_BEEPS) {
		if (elapsedSec < t + beep.holdSec) return IO_TEST_METER_FILL;
		t += beep.holdSec + beep.gapSec;
		if (elapsedSec < t) return 0;
	}
	return 0;
}

export function rmsOf(samples: ArrayLike<number>): number {
	if (samples.length === 0) return 0;
	let sum = 0;
	let n = 0;
	for (let i = 0; i < samples.length; i++) {
		const sample = samples[i];
		if (!Number.isFinite(sample)) continue;
		sum += sample * sample;
		n += 1;
	}
	return n === 0 ? 0 : Math.sqrt(sum / n);
}

/** Linear amplitude to a 0..1 meter fill. -48 dBFS is empty, 0 dBFS is full. */
export function meterFillFromRms(rms: number | null | undefined): number {
	if (rms === null || rms === undefined || !Number.isFinite(rms) || rms <= 0) return 0;
	const db = 20 * Math.log10(Math.min(1, rms));
	return Math.min(1, Math.max(0, (db - METER_FLOOR_DB) / -METER_FLOOR_DB));
}

/** Live bus level and the beep envelope. The taller one wins. */
export function ioMeterFill(rms: number | null | undefined, envelope: number): number {
	const fromSignal = meterFillFromRms(rms);
	const fromBeep = Number.isFinite(envelope) ? Math.min(1, Math.max(0, envelope)) : 0;
	return Math.max(fromSignal, fromBeep);
}

/** Gate a sine into the three beeps. Returns the context time the pattern ends. */
export function scheduleIoTestBeeps(osc: OscillatorNode, gain: AudioParam, startSec: number): number {
	let t = startSec;
	for (const beep of IO_TEST_BEEPS) {
		const attack = Math.min(0.008, beep.holdSec / 3);
		osc.frequency.setValueAtTime(beep.hz, t);
		gain.setValueAtTime(0, t);
		gain.linearRampToValueAtTime(IO_TEST_GAIN, t + attack);
		gain.setValueAtTime(IO_TEST_GAIN, t + beep.holdSec - attack);
		gain.linearRampToValueAtTime(0, t + beep.holdSec);
		t += beep.holdSec + beep.gapSec;
	}
	return t;
}

/**
 * The device a Test on `bus` plays on. In split L/R the cue is the right
 * channel of MAIN's output and no separate CUE device is picked, so a CUE Test
 * goes to MAIN's device ('default', the system row, when MAIN follows the OS).
 * Otherwise CUE needs its own pick; null means none, and the test is refused.
 */
export function ioTestDeviceId(
	bus: IoTestBus,
	hp: { output_mode: string; selected_output_device_id: string | null; selected_master_output_device_id: string | null }
): string | null {
	if (bus !== 'cue') return hp.selected_master_output_device_id;
	if (hp.output_mode === 'split_cable') return hp.selected_master_output_device_id ?? 'default';
	return hp.selected_output_device_id;
}

/** AUDIO IN measures the input the operator picked, never whatever microphone
 * the OS hands over by default (the app's own input path prefers another). */
export const NO_INPUT_PICKED = 'No audio input is picked. Pick AUDIO IN, then test again.';

/** Why a Test on `bus` cannot run with the picks shown, or null when it can. */
export function ioTestRefusal(bus: IoTestBus, hp: { selected_input_device_id: string | null }): string | null {
	if (bus === 'input' && (hp.selected_input_device_id === null || hp.selected_input_device_id === '')) return NO_INPUT_PICKED;
	return null;
}
