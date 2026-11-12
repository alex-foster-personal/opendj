/**
 * Beat FX unit port (PADFX-11718): the seam Pad FX sends through. The beat FX
 * unit graph and its mirror field are #9783; that PR installs the real port,
 * whose `availability` reads the unit flag and refusal reason off the deck
 * mirror and whose `send` routes through runPerformanceCommandFromUi, so HTTP
 * `POST /api/v1/commands` and the CLI drive the same path.
 *
 * There is no default port and no silent no-op: with nothing installed every
 * Pad FX control is disabled with BEAT_FX_UNIT_NOT_WIRED_REASON and a send throws.
 */
import type { BeatFxCommand, BeatFxDeckTarget, BeatFxReleaseCommand } from './beat-fx';

export type BeatFxUnitAvailability = { enabled: true } | { enabled: false; reason: string };
export interface BeatFxUnitPort {
	availability(deck: BeatFxDeckTarget): BeatFxUnitAvailability;
	send(command: BeatFxCommand | BeatFxReleaseCommand): void;
}

export const BEAT_FX_UNIT_NOT_WIRED_REASON =
	'Pad FX needs the beat FX unit, which is not wired into the audio graph yet (#9783)';

const holder: { port: BeatFxUnitPort | null } = $state({ port: null });

/** Install the one port; returns its uninstall. Throws if one is installed. */
export function installBeatFxUnitPort(port: BeatFxUnitPort): () => void {
	if (holder.port !== null) throw new Error('a beat FX unit port is already installed');
	holder.port = port;
	return () => {
		if (holder.port === port) holder.port = null;
	};
}

export function beatFxUnitAvailability(deck: BeatFxDeckTarget): BeatFxUnitAvailability {
	const port = holder.port;
	if (port === null) return { enabled: false, reason: BEAT_FX_UNIT_NOT_WIRED_REASON };
	return port.availability(deck);
}

/** Send one command for `deck`; throws the refusal reason when the unit is unavailable. */
export function sendBeatFxUnitCommand(
	deck: BeatFxDeckTarget,
	command: BeatFxCommand | BeatFxReleaseCommand
): void {
	const availability = beatFxUnitAvailability(deck);
	if (!availability.enabled) throw new Error(availability.reason);
	const port = holder.port;
	if (port === null) throw new Error(BEAT_FX_UNIT_NOT_WIRED_REASON);
	port.send(command);
}
