/**
 * CMDK-17 (issue #6092): the master deck's pairs for the Cmd K bar, read when
 * the bar opens. One `listPairingsFor` (two GETs) for the edges, then one
 * `latestSyncSnapshot` (two GETs) per partner to tell aligned from loose. A
 * failed read is shown as a failure, never as "no pairs".
 */
import { latestSyncSnapshot, listPairingsFor } from './pairing-capture';
import { partnersOf, type CmdkPair } from './cmdk-pairs';

export type PairsState =
	| { status: 'idle' }
	| { status: 'loading'; master: string }
	| { status: 'ready'; master: string; pairs: CmdkPair[] }
	| { status: 'error'; master: string; message: string };

let _state = $state<PairsState>({ status: 'idle' });
let _request = 0;

/** Pure read of the latest answer. */
export function cmdkPairs(): PairsState {
	return _state;
}

/**
 * Ask for `master`'s pairs (null: no master deck, so none). Mutates rune
 * state: call from an event, never a `$derived`. Only the latest request
 * publishes, so a slow answer for a previous master never lands.
 */
export function requestCmdkPairs(master: string | null): void {
	const request = ++_request;
	if (master === null) {
		_state = { status: 'idle' };
		return;
	}
	// Every request publishes loading, even for the same master: a row held from before the refresh would stay
	// pickable against a removed edge or an old sync snapshot until this request answers (Sol P1, #6691).
	_state = { status: 'loading', master };
	void (async () => {
		try {
			const ids = partnersOf(await listPairingsFor(master), master);
			const pairs = await Promise.all(
				ids.map(async (stable_id): Promise<CmdkPair> => {
					const snap = await latestSyncSnapshot(master, stable_id);
					return {
						stable_id,
						snapshot:
							snap === null
								? null
								: {
										aTempoRatio: snap.aTempoRatio,
										bTempoRatio: snap.bTempoRatio,
										aPositionMs: snap.aPositionMs,
										bPositionMs: snap.bPositionMs
									}
					};
				})
			);
			if (request === _request) _state = { status: 'ready', master, pairs };
		} catch (error) {
			if (request === _request) {
				_state = { status: 'error', master, message: error instanceof Error ? error.message : String(error) };
			}
		}
	})();
}
