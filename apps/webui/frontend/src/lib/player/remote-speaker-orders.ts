/**
 * Remote speaker (I18): the page half of the engine's command orders.
 *
 * The engine routes under /api/v1/performance/remote-speaker send these
 * commands to the open performance page through the AGENT-03 command queue
 * (`agent-orders.ts` hands every `remote_speaker_*` type here). The panel
 * calls the same routes, so UI and HTTP drive one path. The mapping from route
 * to command lives in `routes/performance_remote_speaker.py`
 * (REMOTE_SPEAKER_PAGE_COMMANDS), and its test reads this file for each type.
 *
 * Kept out of performance-ipc.svelte.ts on purpose: like SET-12's
 * `record_master_tap`, these commands carry no performance state, and that
 * file is a single-owner hotspot.
 */
import { REMOTE_SPEAKER_OFF_MESSAGE, isSpeakerAddress, isSpeakerCode } from './remote-speaker-core';
import type { RemoteSpeakerFlag } from './remote-speaker-flag';
import {
	answerSpeakerOffer,
	cancelSpeakerAnswer,
	disableSpeaker,
	enableSpeaker,
	linkToSpeaker,
	unlinkFromSpeaker,
	type EngineLink
} from './remote-speaker';

export const REMOTE_SPEAKER_COMMAND_TYPES = Object.freeze([
	'remote_speaker_receive',
	'remote_speaker_answer',
	'remote_speaker_answer_cancel',
	'remote_speaker_receive_stop',
	'remote_speaker_link',
	'remote_speaker_unlink'
] as const);
export type RemoteSpeakerCommandType = (typeof REMOTE_SPEAKER_COMMAND_TYPES)[number];

export function isRemoteSpeakerCommand(type: unknown): type is RemoteSpeakerCommandType {
	return typeof type === 'string' && (REMOTE_SPEAKER_COMMAND_TYPES as readonly string[]).includes(type);
}

export interface RemoteSpeakerEnv {
	flag: RemoteSpeakerFlag;
	engine: EngineLink;
	/** MAIN's context and node. Throws with the reason when there is none. */
	masterTap: () => { context: AudioContext; node: AudioNode };
	/** The selected MAIN output device id, or null for the system default. */
	masterSinkId: () => string | null;
}

/** Run one remote speaker command; the answer command returns its SDP. */
export async function runRemoteSpeakerCommand(
	command: Record<string, unknown>,
	env: RemoteSpeakerEnv
): Promise<{ sdp: string } | null> {
	const type = command.type;
	if (!isRemoteSpeakerCommand(type)) throw new TypeError(`unknown remote speaker command ${String(type)}`);
	// Stopping is always allowed: with the feature off nothing runs, so it is a no-op.
	if (type === 'remote_speaker_receive_stop') {
		disableSpeaker();
		return null;
	}
	if (type === 'remote_speaker_unlink') {
		unlinkFromSpeaker();
		return null;
	}
	if (type === 'remote_speaker_answer_cancel') {
		if (typeof command.link_id !== 'string') throw new TypeError('remote_speaker_answer_cancel needs link_id');
		cancelSpeakerAnswer(command.link_id);
		return null;
	}
	if (env.flag !== 'on') throw new Error(REMOTE_SPEAKER_OFF_MESSAGE);
	switch (type) {
		case 'remote_speaker_receive':
			if (typeof command.epoch !== 'string') throw new TypeError('remote_speaker_receive needs epoch');
			enableSpeaker(command.epoch, env.engine);
			return null;
		case 'remote_speaker_answer': {
			if (typeof command.sdp !== 'string') throw new TypeError('remote_speaker_answer needs sdp');
			if (typeof command.epoch !== 'string' || typeof command.link_id !== 'string') {
				throw new TypeError('remote_speaker_answer needs epoch and link_id');
			}
			return {
				sdp: await answerSpeakerOffer({
					sdp: command.sdp,
					epoch: command.epoch,
					linkId: command.link_id,
					sinkId: env.masterSinkId(),
					engine: env.engine
				})
			};
		}
		case 'remote_speaker_link': {
			if (!isSpeakerAddress(command.address)) throw new TypeError('remote_speaker_link needs address host:port');
			if (!isSpeakerCode(command.code)) throw new TypeError('remote_speaker_link needs the 6-digit code');
			if (typeof command.link_id !== 'string') throw new TypeError('remote_speaker_link needs link_id');
			const tap = env.masterTap();
			await linkToSpeaker({
				address: command.address,
				code: command.code,
				linkId: command.link_id,
				context: tap.context,
				source: tap.node,
				engine: env.engine
			});
			return null;
		}
	}
}
