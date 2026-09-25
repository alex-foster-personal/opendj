/**
 * Typed calls over the karaoke words router
 * (apps/webui/server/routes/lyrics_words.py, LYR-03 / PR-4a).
 *
 * Its OWN module rather than a section of `./api.ts`, for the same reason
 * `api-cloudsync.ts` and `api-smartlists.ts` are their own modules: `api.ts`
 * is the shared hotspot every panel already imports, it was already 501 lines
 * before this surface existed, and the quality ratchet counts frontend files
 * over 600. Same client instance, same `unwrap`, same
 * alias-the-generated-schema discipline -- only the file boundary differs.
 *
 * SCOPE: exactly the two calls PR-4a's UI makes. The router also serves the
 * triage listing, the verdict summary, the source-order config, the job queue
 * and the purge, all covered by tests/webui/test_words_lyrics.py and reachable
 * by an agent over HTTP today; their typed wrappers land in PR-4b NEXT TO the
 * panels that call them, because a client function with no caller is dead code
 * the dead-export ratchet is right to count.
 *
 * Types are ALIASES of the generated schemas, so each field's server-side
 * `description` reaches the UI as the hover text a numeric readout must carry.
 *
 * Mind the path: `/api/v1/tracks/{stable_id}/lyrics/words` is this timeline,
 * while `/api/v1/tracks/{stable_id}/lyrics` (no `/words`) is an older,
 * unrelated route serving cached LRCLIB line text - which is why the models
 * are named Karaoke and Coverage rather than Lyric.
 */
import type { components } from './api-types';
import { ApiError, api, unwrap } from './api/client';
import { isUsbTrackId, refuseStickWrite } from './rb/track-source';

export type KaraokeWord = components['schemas']['KaraokeWordOut'];
export type KaraokeTrack = components['schemas']['KaraokeTrackOut'];
export type CoverageVerdict = components['schemas']['CoverageVerdictOut'];

/** The verdicts `apps/lyrics/verdict.py` stamps: the only hand-written shape
 * here, because the column is an open string in the schema and the alias
 * cannot narrow it. A WRITER must pick a real member; the read side keeps the
 * server's open string, so a value the daemon invents later renders as itself
 * instead of crashing a lookup. */
export type LyricVerdictValue = 'vocal' | 'sparse' | 'no-lyrics' | 'unknown';

/** One track's karaoke timeline, or null when the pipeline has nothing for it.
 *
 * Null means 404 and ONLY 404. The route answers 404 both for "no verdict row"
 * and for "row exists, words artifact unhydratable", and no caller can act
 * differently on those, so both collapse to the honest "nothing to show".
 * Every other status still throws `ApiError`: a 500 or a 422 is a real failure
 * and must never render as an empty track. */
export async function getTrackLyricsWords(
	stableId: string,
	opts: { includeLines?: boolean } = {}
): Promise<KaraokeTrack | null> {
	// Spec 4b: a stick track has no lyrics route, so it is the no-lyrics state.
	if (isUsbTrackId(stableId)) return null;
	try {
		return await unwrap(
			api.GET('/api/v1/tracks/{stable_id}/lyrics/words', {
				params: {
					path: { stable_id: stableId },
					query: opts.includeLines === true ? { include: 'lines' } : {}
				}
			})
		);
	} catch (error) {
		if (error instanceof ApiError && error.status === 404) return null;
		throw error;
	}
}

/** Record the human's disagreement with the computed verdict, or clear it with
 * `null`. Returns the row the server wrote, so callers push THAT into the
 * shared cache rather than guessing what it now says. */
export async function putLyricOverride(
	stableId: string,
	override: LyricVerdictValue | null,
	note?: string
): Promise<CoverageVerdict> {
	// Spec decision 2: nothing is ever written for a stick track.
	refuseStickWrite(stableId, 'lyric override');
	return unwrap(
		api.PUT('/api/v1/tracks/{stable_id}/lyrics/override', {
			params: { path: { stable_id: stableId } },
			body: { override, note: note ?? null }
		})
	);
}
