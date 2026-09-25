/**
 * PARITY-10: give an UNMAPPED track the beatgrid apps/analysis measured for it.
 *
 * `GET /anlz` answers 200 for a locally imported file
 * (rb_vendor.empty_anlz_payload), so such a deck loads with `beats: []` - and
 * both grid readers (`_quantizeGrid` for transport, `_requireBeatGrid` for Beat
 * Sync and beat loops) then have nothing to work with. Landing the
 * analysis-derived grid on `st.anlz` is what brings beat ticks, quantize, beat
 * loops and sync alive together, because they all read that one field.
 *
 * Deferred, exactly like the stem-bundle upgrade: `load()` kicks this off AFTER
 * the deck can already play, so the critical path costs nothing. Every exit is
 * a settled answer - the deck is never left claiming a grid it does not have.
 *
 * ANLZ is always preferred. A track that already has a real PQTZ grid never
 * pays for the /rb-meta probe, and the merge itself refuses to overwrite one.
 */
import { fetchAnlz, fetchRbMeta, RbApiError } from '$lib/rb/api-rb';
import { analysisSourceState } from '$lib/rb/analysis-source-state.svelte';
import { fetchBeatgridFallback } from '$lib/rb/beatgrid-fallback-api';
import {
	hasAnlzBeatgrid,
	hasNoVendorAnlzPayload,
	shouldUseBeatgridFallback,
	withFallbackBeatgrid
} from '$lib/rb/beatgrid-fallback';
import { recordPerfTiming } from '$lib/rb/perf-event-log';
import { isUsbTrackId } from '$lib/rb/track-source';
import { pushToast } from '$lib/stores.svelte';
import type { AnlzData } from '$lib/rb/anlz-types';
import type { DeckState } from '$lib/rb/deck-state-types';

type DeckId = DeckState['deck_id'];

/**
 * Fetch and land the fallback grid for one loaded deck.
 *
 * `isStale` is the caller's load-token check: the deck can be replaced across
 * either request, and a swapped-out deck must never take the previous track's
 * grid. It is asked before the first read and after every await.
 *
 * Resolves rather than rejects on a backend refusal, because callers fire this
 * with `void` and an escaping rejection would be an unhandled promise.
 *
 * `onSettled`, if given, runs once this attempt has settled either way. This
 * module has no notion of Beat Sync or the sync master/follower machinery -
 * that lives in audio-engine.svelte.ts - so a caller that cares whether an
 * already-playing follower needs re-locking, or abandoning, passes the hook
 * rather than this module reaching into engine internals. It is called
 * `(deck, landed, publish)`: `landed` is false for every settlement that is
 * not a fresh grid (rekordbox-mapped, no analysis row yet, or a real backend
 * failure), and `publish` is the thunk that actually assigns `st.anlz` - a
 * `landed` caller MUST invoke it (typically as the first thing inside its own
 * scope claim), since this module defers the assignment to the caller rather
 * than publish before the caller has claimed whatever scope reconciliation
 * needs, which would let other code observe the grid as landed before
 * reconciliation has run. A caller that has none still gets the grid: `publish`
 * runs immediately when `landed` is true and no `onSettled` was given. Any
 * rejection from `onSettled` is handled the same as a transport failure: loud,
 * never silent, and never escapes this `void`-called function.
 */
export async function upgradeDeckBeatgrid(
	deck: DeckId,
	stableId: string,
	st: DeckState,
	isStale: () => boolean,
	onSettled?: (deck: DeckId, landed: boolean, publish: () => void) => void | Promise<void>
): Promise<void> {
	// A candidate that lost the swap race left st.anlz owned by another track.
	if (isStale()) return;
	const loaded = st.anlz;
	if (loaded === null || hasAnlzBeatgrid(loaded)) return;
	const t0 = performance.now();
	const stages: Record<string, number> = {};
	const time = async <T>(name: string, work: Promise<T>): Promise<T> => {
		const started = performance.now();
		try {
			return await work;
		} finally {
			stages[name] = Math.round(performance.now() - started);
		}
	};
	const settle = async (landed: boolean, publish: () => void = () => {}): Promise<void> => {
		if (!onSettled) {
			if (landed) publish();
			return;
		}
		try {
			await onSettled(deck, landed, publish);
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			pushToast(`Deck ${deck} beatgrid arrived but could not sync - ${message}`, 'error');
		}
	};
	// publish can run much later than this point: onSettled (audio-engine's
	// _resyncAfterBeatgridUpgrade) queues it behind whatever else already
	// holds the deck's scoped command slot. A replacement load can win that
	// queue and swap st.anlz before this callback finally runs, so both the
	// staleness check and the current-grid read are repeated inside the
	// thunk itself rather than trusted from the closure captured before the
	// wait began.
	const publishGrid = (nextAnlz: AnlzData) => (): void => {
		if (isStale()) return;
		const latest = st.anlz;
		if (latest === null || hasAnlzBeatgrid(latest)) return;
		// Only the beatgrid, never the whole captured payload: a refreshHotCues
		// that ran while this publish waited for its scoped slot has already
		// republished st.anlz with newer cues and waveform metadata for the SAME
		// track, so assigning nextAnlz wholesale would roll those back and leave
		// the wave row's cue markers stale against the hot-cue bank
		// (PR #765 'Merge the fallback grid into the latest ANLZ payload').
		st.anlz = { ...latest, beatgrid: nextAnlz.beatgrid };
		stages.total = Math.round(performance.now() - t0);
		recordPerfTiming(`deck-beatgrid-fallback sid=${stableId.slice(0, 12)}`, stages, deck);
	};
	// A vendor mapping can land while /beatgrid-fallback is in flight: the
	// authoritative PQTZ grid now exists server-side even though st.anlz has
	// no way to know that on its own. ANLZ is always preferred, so a fallback
	// grid must never install over it - re-fetch /anlz instead and use ITS
	// grid, rather than leaving the deck gridless until the DJ happens to
	// reload the track. Shared by both the 200 response's own anlz_available
	// field and the 404 detail's field of the same name (a mapping can land
	// with no usable apps.analysis record too - see the catch block below).
	// bypassCache=true: this deck's own load already fetched this exact URL
	// with the empty payload minutes earlier, and a cached replay would settle
	// gridless despite the grid now existing. /anlz is `private, no-cache` with
	// an ETag today, so a replay would have to survive a revalidation; it was
	// `public, max-age=3600` when this was written, and forcing the read keeps
	// the guarantee here rather than in the header.
	const settleFromAnlzRefetch = async (): Promise<void> => {
		const refreshed = await time('fetchAnlzRefetch', fetchAnlz(stableId, undefined, true));
		if (isStale()) return;
		if (hasAnlzBeatgrid(refreshed)) {
			return await settle(true, () => {
				// publish itself can run much later than this point (see
				// publishGrid's own comment above): onSettled queues it behind
				// whatever else already holds the deck's scoped command slot,
				// and a source switch can land in that window. /anlz resolves
				// `beatgrid_source` server-side AT FETCH TIME
				// (rb_assets.py `_resolve_beatgrid_source`), so `refreshed` is
				// only a valid answer for the source that was live when this
				// fetch was issued - re-checking against the LIVE source at
				// publish time is required here too, the same as the fallback
				// path below, or a switch that lands in this window merges a
				// grid stamped under the OLD source into a payload that by
				// then belongs to the new one (discussion_r3976638762 P1
				// BLOCKING).
				if (analysisSourceState.features.beatgrid !== refreshed.beatgrid_source) return;
				publishGrid(refreshed)();
			});
		}
		return await settle(false);
	};
	// Spec 4b: a stick track's only grid is the stick's own PQTZ. It has no
	// rb-meta, fallback grid or analysis record to reach for, so a gridless
	// stick deck settles gridless here (the same answer as a track with no
	// analysis yet) and makes no request.
	if (isUsbTrackId(stableId)) return await settle(false);
	try {
		// The only honest source of "this track has no rekordbox mapping":
		// RbMetaOut.vendor (apps/webui/server/routes/rb_assets.py). The empty
		// /anlz payload itself does not say, and it must not be guessed at.
		const meta = await time('fetchRbMeta', fetchRbMeta(stableId));
		if (isStale()) return;
		const gate = {
			anlzErrorCode: st.anlz_error,
			anlz: st.anlz,
			vendor: meta.vendor,
			effectiveSource: analysisSourceState.features.beatgrid
		};
		if (!shouldUseBeatgridFallback(gate)) {
			// A vendor mapping can land BEFORE this fetchRbMeta() call itself
			// resolves, not only while /beatgrid-fallback is later in flight (see
			// settleFromAnlzRefetch's own callers below): st.anlz is still this
			// deck's stale local empty payload, but vendor now reads non-local, so
			// shouldUseBeatgridFallback correctly refuses the LOCAL-only synthetic
			// fallback. That must not mean giving up - the vendor now holds the
			// authoritative grid one /anlz refetch away. Only fires for exactly
			// that stale-local-payload shape; every OTHER refusal reason (a real
			// anlz_error, no /anlz answer yet, or a grid already present) still
			// settles false immediately, unchanged (issue #734 send-back
			// r3912960736).
			if (gate.vendor !== 'local' && gate.anlzErrorCode === null && gate.anlz !== null && !hasAnlzBeatgrid(gate.anlz)) {
				return await settleFromAnlzRefetch();
			}
			return await settle(false);
		}
		const fallback = await time('fetchBeatgridFallback', fetchBeatgridFallback(stableId));
		if (isStale()) return;
		if (fallback.anlz_available) return await settleFromAnlzRefetch();
		// Re-read live, not gate.effectiveSource: a switch back to rekordbox
		// while this fetch was in flight must not land an own-derived grid
		// after the fact (discussion_r3972682719 P1 BLOCKING).
		const liveAnlz = st.anlz;
		const noVendorAnlz =
			liveAnlz !== null && !hasAnlzBeatgrid(liveAnlz) && hasNoVendorAnlzPayload(liveAnlz);
		if (analysisSourceState.features.beatgrid !== 'own' && !noVendorAnlz) {
			return await settle(false);
		}
		// Re-read across the awaits: refreshHotCues can have replaced st.anlz,
		// and withFallbackBeatgrid throws rather than demote a real grid.
		const current = st.anlz;
		if (current === null || hasAnlzBeatgrid(current)) return;
		const fallbackAnlz = withFallbackBeatgrid(current, fallback);
		// publish itself can run much later than this point (see publishGrid's
		// own comment above): onSettled queues it behind whatever else already
		// holds the deck's scoped command slot, and a source switch back to
		// rekordbox can land in that window too, same as the live check just
		// above at deferral time - re-check it again at the moment of the
		// actual write, or a switch that arrives between deferral and
		// publication still lands the OWN fallback grid it raced against
		// (discussion_r3973991956 P1 BLOCKING). settleFromAnlzRefetch's own
		// publish thunk above carries the analogous check against
		// `refreshed.beatgrid_source`, since /anlz's grid IS source-dependent
		// even though a vendor mapping landing is not.
		await settle(true, () => {
			const latestAnlz = st.anlz;
			const allowNoVendorPublish =
				latestAnlz !== null &&
				!hasAnlzBeatgrid(latestAnlz) &&
				hasNoVendorAnlzPayload(latestAnlz);
			if (analysisSourceState.features.beatgrid !== 'own' && !allowNoVendorPublish) return;
			publishGrid(fallbackAnlz)();
		});
	} catch (error) {
		if (isStale()) return;
		if (error instanceof RbApiError && error.code === 'BEATGRID_FALLBACK_NOT_FOUND') {
			// A settled backend answer, not a failure: this means apps/analysis
			// has not run on this track yet, and a grid is never invented. The
			// deck keeps playing with no grid - quantize simply has no effect
			// (see grid-features.effectiveQuantize). Any OTHER RbApiError (a
			// 5xx, a backend-reported shape mismatch, an unrelated rb-meta
			// failure) is a real failure, not this settled case, and falls
			// through to the loud path below.
			stages.failedAt = Math.round(performance.now() - t0);
			recordPerfTiming(
				`deck-beatgrid-fallback-none sid=${stableId.slice(0, 12)} ${error.code}`,
				stages,
				deck
			);
			// The same mid-flight mapping race as the 200 branch above, just
			// surfaced through a 404 this time: apps.analysis has nothing
			// usable, but a vendor mapping (and its real ANLZ grid) landed
			// while this request was in flight.
			const detail = (error.body as { detail?: { anlz_available?: boolean } } | null)?.detail;
			if (detail?.anlz_available === true) {
				// Unlike the 200 branch's own settleFromAnlzRefetch() call (still
				// inside the outer try), this one runs from INSIDE the catch, so a
				// rejection here (mapping disappears again, a transient 5xx) is not
				// caught by that same try/catch - wrap it explicitly, loud and
				// settled the same as any other real failure.
				try {
					return await settleFromAnlzRefetch();
				} catch (refetchError) {
					if (isStale()) return;
					const message =
						refetchError instanceof Error ? refetchError.message : String(refetchError);
					pushToast(`Deck ${deck} beatgrid fallback failed - ${message}`, 'error');
					return await settle(false);
				}
			}
			return await settle(false);
		}
		// Not a settled "no analysis yet" answer - a shape, transport, or
		// backend failure. Loud, never silent.
		const message = error instanceof Error ? error.message : String(error);
		pushToast(`Deck ${deck} beatgrid fallback failed - ${message}`, 'error');
		await settle(false);
	}
}
