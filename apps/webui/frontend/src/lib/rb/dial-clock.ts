/**
 * DIALCLOCK-8341: dial positions as clock times, the way DJs say them ("filter to
 * 3 o'clock"). One mapping for the UI, the docs and the agent surfaces.
 *
 * Canonical forms:
 *  - internal and on the wire: the numeric dial value, 0..1, 0.5 = centre;
 *  - in human and agent text: "H o'clock" ("3 o'clock"), "H:MM" between hours.
 *
 * The geometry is the app's own dial (`Knob.svelte`): a 270-degree sweep,
 * -135..+135 degrees from straight up, like rekordbox's on-screen knobs. Each
 * clock hour is 30 degrees, so 12 o'clock is the centre (0.5), 3 o'clock is
 * 0.8333, 9 o'clock is 0.1667, full left (0) is 7:30 and full right (1) is
 * 4:30. A hardware DJM pot sweeps about 300 degrees (7 to 5 o'clock); this map
 * follows what the screen draws, so a dial set to "3 o'clock" LOOKS like 3.
 *
 * Accepted spellings: "3", "3pm", "3 am", "3 o'clock", "3oclock", "3:00",
 * "15:00", "4:30", "7:30pm". AM/PM and 24-hour clocks are only a way to say
 * "this is a time": 3pm, 3am, 15:00 and 3 o'clock are the same position.
 * Positions in the gap at the bottom (after 4:30, before 7:30) are refused.
 * The Python twin is `apps/opendj_cli/kinds.py` (`dial_clock_value`).
 */

/** Total dial travel in degrees, centred on 12 o'clock. Shared with Knob.svelte. */
export const DIAL_SWEEP_DEG = 270;
const DEG_PER_HOUR = 30;
const HALF_SWEEP_HOURS = DIAL_SWEEP_DEG / 2 / DEG_PER_HOUR; // 4.5

const CLOCK_PATTERN = /^\s*(\d{1,2})(?::(\d{2}))?\s*(?:o'?\s*clock|am|pm|a\.m\.|p\.m\.)?\s*$/i;

/** Signed hours from 12 o'clock (-4.5..+4.5), or a thrown reason. */
function _signedHours(text: string): number {
	const match = CLOCK_PATTERN.exec(text);
	if (match === null) {
		throw new TypeError(`dial clock time must look like 3, 3pm, 3 o'clock, 3:00 or 15:00; got ${JSON.stringify(text)}`);
	}
	const hour = Number(match[1]);
	const minute = match[2] === undefined ? 0 : Number(match[2]);
	if (hour > 24 || minute > 59) throw new TypeError(`dial clock time is not a time of day: ${JSON.stringify(text)}`);
	const onDial = ((hour % 12) + minute / 60) % 12; // 0..<12, 0 = 12 o'clock
	const signed = onDial > 6 ? onDial - 12 : onDial;
	if (Math.abs(signed) > HALF_SWEEP_HOURS + 1e-9) {
		throw new RangeError(
			`dial clock time ${JSON.stringify(text)} is past the dial's stops: it turns from 7:30 (full left, 0) through 12 o'clock (centre, 0.5) to 4:30 (full right, 1)`
		);
	}
	return signed;
}

/** "3 o'clock" -> 0.8333. Throws on anything that is not a clock time on the dial. */
export function dialValueFromClock(text: string): number {
	return 0.5 + _signedHours(text) / (2 * HALF_SWEEP_HOURS);
}

/** 0.8333 -> "3 o'clock"; between hours "4:30". Rounded to the nearest 5 minutes. */
export function dialClockLabel(value: number): string {
	if (!Number.isFinite(value) || value < 0 || value > 1) {
		throw new RangeError(`dial value must be a number in 0..1; got ${String(value)}`);
	}
	const signedMinutes = Math.round(((value - 0.5) * 2 * HALF_SWEEP_HOURS * 60) / 5) * 5;
	const minutesPastTwelve = (signedMinutes + 720) % 720;
	const hour = Math.floor(minutesPastTwelve / 60) || 12;
	const minute = minutesPastTwelve % 60;
	return minute === 0 ? `${hour} o'clock` : `${hour}:${String(minute).padStart(2, '0')}`;
}
