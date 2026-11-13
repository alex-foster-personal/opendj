/**
 * The shared echo kernel (`fx/echo-kernel.ts`) behind the unit's `BeatFxKernel` slot (issue #9782).
 * Hosts the one echo line on main; no second echo. Delay, echo and pingpong each build it in
 * their own kernel mode (issue #13843). A tempo change retunes through the kernel's own
 * stepped-line crossfade, never a delayTime slide.
 */
import { ECHO_MODES, buildEchoKernel, type EchoKernel, type EchoMode } from '../echo-kernel';
import { TIME_FX_CATALOG } from '../time-fx-catalog';
import type { BeatFxKernel } from './kernel-host';

/** Loop numbers mimicked from the color ECHO (color-fx-graph.ts ECHO_*). */
export const BEAT_FX_ECHO_FEEDBACK = 0.55;
export const BEAT_FX_ECHO_LOOP_HP_HZ = 180;
export const BEAT_FX_ECHO_LOOP_LP_HZ = 3000;
/** Longest echo time any time-catalog row allows. */
export const BEAT_FX_ECHO_MAX_DELAY_SEC =
	Math.max(...TIME_FX_CATALOG.map((r) => r.timeMsMax)) / 1000;
/** Depth moves ramp this long (spec 01 section 4.3 Ramps row). */
const DEPTH_RAMP_SEC = 0.012;

/** The unit's echo slot: the BeatFxKernel contract plus the echo kernel it hosts. */
export interface EchoSlotKernel extends BeatFxKernel {
	readonly echo: EchoKernel;
}

/** The echo kernel mode a unit effect runs in; throws naming the effect when it is not an echo mode. */
export function echoSlotMode(effect: string): EchoMode {
	if (!(ECHO_MODES as readonly string[]).includes(effect)) {
		throw new TypeError(
			`beat_fx effect ${effect} is not an echo kernel mode (${ECHO_MODES.join(', ')})`
		);
	}
	return effect as EchoMode;
}

/** Build the echo kernel in `mode` for the unit's slot: send -> echo loop -> wet depth gain (starts at 0). */
export function createEchoSlotKernel(ctx: BaseAudioContext, mode: EchoMode): EchoSlotKernel {
	const echo = buildEchoKernel(ctx, {
		mode,
		maxDelaySec: BEAT_FX_ECHO_MAX_DELAY_SEC,
		feedback: BEAT_FX_ECHO_FEEDBACK,
		loopHpHz: BEAT_FX_ECHO_LOOP_HP_HZ,
		loopLpHz: BEAT_FX_ECHO_LOOP_LP_HZ
	});
	const wet = ctx.createGain();
	wet.gain.value = 0;
	echo.output.connect(wet);
	let depthNow = 0;
	return {
		echo,
		input: echo.input,
		output: wet,
		setTiming: (seconds, at) => echo.setTime(seconds, at),
		setDepth: (depth, at) => {
			wet.gain.cancelScheduledValues(at);
			wet.gain.setValueAtTime(depthNow, at);
			wet.gain.linearRampToValueAtTime(depth, at + DEPTH_RAMP_SEC);
			depthNow = depth;
		},
		dispose: () => {
			echo.dispose();
			wet.disconnect();
		}
	};
}
