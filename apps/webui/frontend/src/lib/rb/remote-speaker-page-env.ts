/**
 * Remote speaker (I18): the live page's environment for its command orders.
 *
 * Loaded lazily by `agent-orders.ts` the first time a `remote_speaker_*`
 * order arrives, so a page that never links pays nothing for it.
 */
import { API_BASE } from '$lib/api/base';
import { remoteSpeakerBuildFlag } from '$lib/player/remote-speaker-flag';
import type { RemoteSpeakerEnv } from '$lib/player/remote-speaker-orders';
import { masterMixUnavailableReason } from '$lib/sets/record-input-choice';
import { masterMixTapPoint } from '$lib/sets/master-mix-capture';

/** `masterSinkId` comes from the caller, which already holds the performance
 * state, so this module stays off the performance-ipc import fan-in. */
export function remoteSpeakerPageEnv(masterSinkId: RemoteSpeakerEnv['masterSinkId']): RemoteSpeakerEnv {
	return {
		flag: remoteSpeakerBuildFlag(),
		engine: { apiBase: API_BASE, fetch: (input, init) => fetch(input, init) },
		masterTap: () => {
			// The Rust engine mixes outside the page: there is no MAIN here to tap.
			if (masterMixUnavailableReason() !== null) {
				throw new Error(
					'The Rust engine (preview) mixes outside the page, so its MAIN cannot be sent to a remote speaker yet. Switch Settings > Audio engine to Web Audio.'
				);
			}
			const point = masterMixTapPoint(true);
			if (point === null) throw new Error('the audio engine has no master bus to send');
			return point;
		},
		masterSinkId
	};
}
