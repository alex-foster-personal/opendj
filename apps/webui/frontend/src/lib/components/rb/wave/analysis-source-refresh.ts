/**
 * PARITY-02 rbx-vs-own deck refresh, split out of anlz-cache.svelte.ts to
 * keep that file under the repo's 600-line file-size ratchet.
 *
 * Takes the cache's own primitives as PORTS (same pattern as
 * `BeatgridResyncPorts`, beatgrid-resync-guards.ts) rather than importing
 * `./anlz-cache.svelte` directly: that module already needs to import THIS
 * one for the public `refreshAnalysisSourceDecks` wrapper it exposes, and a
 * reverse import here would close a two-file cycle the frontend import-graph
 * ratchet forbids. `anlz-cache.svelte.ts` supplies its own
 * `invalidateAllAnlzCacheEntries`/`refreshAnlzCacheEntry`/
 * `notifyGridlessSettlement` as the ports argument, so this module never
 * touches that file's private `_cache` or `_authoritativeGridSink` either.
 *
 * No $state/$derived here, so this is a plain .ts file, not .svelte.ts.
 */
import { hasAnlzBeatgrid } from '$lib/rb/beatgrid-fallback';
import type { Track } from '$lib/api';
import {
	bumpAnlzFetchGeneration,
	currentAnlzFetchGeneration
} from '$lib/rb/anlz-fetch-generation';
import type { AnlzData } from '$lib/rb/anlz-types';

// rbx-lane BPM/key provenance is one of several concrete sources (rekordbox,
// manual, inferred, webui), not a binary rbx/own one, so "not literally
// rekordbox" is NOT the same claim as "own" - an RBX-lane field can
// legitimately be sourced from MIK, djay or another non-rekordbox writer
// (discussion_r3974235445 P1 BLOCKING).
const _OWN_BACKEND_PREFIX = 'own_';
const _OWN_ANALYSIS_SOURCE = 'own-analysis';
function _isOwnProvenanceSource(source: string): boolean {
	return source === _OWN_ANALYSIS_SOURCE || source.startsWith(_OWN_BACKEND_PREFIX);
}

export type DeckId = 1 | 2 | 3 | 4;

/** The cache primitives this module needs, injected by `anlz-cache.svelte.ts`
 * (see the file docstring above for why this is a ports param, not a direct
 * import). */
export interface AnalysisSourceRefreshPorts {
	invalidateAllAnlzCacheEntries: () => void;
	refreshAnlzCacheEntry: (
		stable_id: string,
		data: AnlzData,
		alreadyScoped?: boolean
	) => void | Promise<void>;
	notifyGridlessSettlement: (
		stable_id: string,
		data: AnlzData,
		alreadyScoped?: boolean
	) => void | Promise<void>;
	/** Reads the CURRENT shared cache entry for a track, or null if none is
	 * ready. Preferred over an arbitrary holder's `decks[deck].anlz` when
	 * merging below: a duplicate-holder track (same track loaded on two
	 * decks) can have a hot-cue write land on the SECOND holder while this
	 * refresh's own fetches are still staged, and that write reaches the
	 * shared cache (refreshHotCues always calls refreshAnlzCacheEntry)
	 * before it reaches every OTHER holder's own `.anlz`. Picking the first
	 * holder found by `holders.find` as the sole base would silently copy
	 * its now-stale cues over both the fresher deck and the cache
	 * (discussion_r3978049105 P1 BLOCKING). */
	getReadyAnlz: (stable_id: string) => AnlzData | null;
	/** Routed through the ports too, not imported straight from
	 * `$lib/rb/api-rb` here: `anlz-cache.svelte.ts` already imports that
	 * module for `fetchAnlz`/`RbApiError`, and a second, separate importer
	 * would add a new edge to `api-rb.ts`'s own fan-in ratchet for no reason
	 * other than this file existing. */
	fetchAnlzBypassingHttpCache: (stable_id: string) => Promise<AnlzData>;
	/** Same injection seam as ``fetchAnlzBypassingHttpCache``: the cache
	 * module's ``api-rb`` binding carries the test/prod ``VITE_API_BASE``,
	 * and a second direct import here would be a separate bundle instance
	 * under node unit tests. */
	fetchTrackBypassingHttpCache: (stable_id: string) => Promise<Track>;
}

export interface AnalysisSourceRefreshDeck {
	stable_id: string | null;
	anlz: AnlzData | null;
	/** The two LANE-OWNED track fields a deck holds. Both are projected per
	 * lane by the read model (`apps/analysis/canonical.py` PROJECTION_FIELDS
	 * maps `bpm` to the beatgrid lane and `key` to the key lane), so
	 * `GET /tracks/{id}` answers with a different value once a lane is on own
	 * - which makes them as source-dependent as the beatgrid itself. */
	bpm: number | null;
	key: string | null;
}

/** Replaces loaded decks without allowing an in-flight stale response to win.
 * Lives here so it can write straight through this cache's own primitives,
 * paired with `invalidateAllAnlzCacheEntries` above.
 *
 * FETCH EVERY DECK FIRST, PUBLISH NOTHING UNTIL ALL LAND: staging the fetches
 * makes the publish phase below synchronous and total. An earlier shape wrote
 * each deck inside its own `Promise.all` callback, so one rejected track
 * (invalid own record, transient 500) left already-resolved decks swapped to
 * the new source while the rest stayed on the old one, with nothing to roll
 * them back - `_adopt` (analysis-source.svelte.ts) leaves `analysisSourceState`
 * on the OLD value on rejection, so the next poll retried forever against a
 * split fleet (discussion_r3968214019 P1 BLOCKING).
 *
 * CACHE FIRST, DECK SECOND: `refreshAnlzCacheEntry`'s sink routes a grid
 * change through `afterBeatgridUpgrade` -> `reconcileAfterBeatgridSettled`,
 * comparing the new grid against the deck's CURRENT one (`sameBeatgrid`,
 * beatgrid-resync-guards.ts). Writing `decks[deck].anlz` first made every
 * switch look like a no-op: waveform and beat numbers moved to the new grid
 * while the audible transport stayed scheduled against the old one
 * (discussion_r3968213995 P1 BLOCKING).
 *
 * THE TRACK ROW IS SOURCE-DEPENDENT TOO. `bpm`/`key` are lane-owned
 * projection fields (`PROJECTION_FIELDS` in apps/analysis/canonical.py), read
 * once at `load()`. Refreshing only the grid left `st.bpm` reporting the
 * pre-switch tempo while the grid and `effective_bpm` reported the new one -
 * an internally inconsistent read model in the one tool whose purpose is
 * comparing the two (discussion_r3969020988 P2 BLOCKING). Staged in the same
 * `Promise.all` as the grid so the all-or-nothing guarantee above covers both.
 *
 * RETURNS THE SOURCE THE SERVER ACTUALLY SERVED, or null when nothing was
 * fetched: `/anlz` resolves rbx-vs-own SERVER-side at fetch time and stamps
 * it on the payload since the daemon can move under the unbounded awaits
 * here (discussion_r3970117741 P1 BLOCKING); a disagreeing staged payload
 * throws before publishing anything.
 *
 * DOES NOT RESOLVE UNTIL EVERY TRIGGERED GRID RECONCILIATION HAS SETTLED, so
 * a caller holding the scheduler's claim can't release it early
 * (discussion_r3970967293 P1 BLOCKING). */
export async function refreshAnalysisSourceDecks(
	deckIds: readonly DeckId[],
	decks: Record<DeckId, AnalysisSourceRefreshDeck>,
	ports: AnalysisSourceRefreshPorts,
	// Checked once, right before publish, after every staged fetch has
	// settled (discussion_r3975326238 P1 BLOCKING): the caller's own
	// post-await supersession check (analysis-source.svelte.ts's `_adopt`)
	// runs AFTER this whole function returns, which is too late - by then the
	// fetches staged below have already been written onto `decks[deck].anlz`
	// and the shared cache. A slower switch that loses a race to a faster,
	// later one must discard its answer HERE, before publishing, or the
	// decks and cache end up holding the loser's bytes while the mirror
	// (`analysisSourceState.deckFeatures`) reports the winner - a split no
	// later poll can detect, since deckFeatures was never advanced past its
	// pre-switch value for the loser. Defaults to "never superseded" for the
	// one other caller (`_drainPendingRecordRefresh`'s record-change refresh,
	// which owns its own generation guard, not a source-switch race) and for
	// every test that calls this directly.
	isSuperseded: () => boolean = () => false
): Promise<'rekordbox' | 'own' | null> {
	// Checked here too, before doing any work, not only at line 182: under
	// sustained scheduler contention a new poll can queue and supersede its
	// predecessor before that predecessor even starts. Without this, every
	// batch still pays the full invalidate-generation-bump-and-fetch cost only
	// to discard its answer at the pre-publication check, and if batches
	// consistently take at least one more poll interval than they have,
	// every one of them is already obsolete by the time it would publish -
	// the watermark can never settle and live controls stay delayed
	// indefinitely (discussion_r3975846958 P1 BLOCKING). The later check
	// stays: a call that was NOT superseded when it started can still lose a
	// race to a newer one while its own fetches are in flight.
	if (isSuperseded()) return null;
	bumpAnlzFetchGeneration();
	const generationAtBatch = currentAnlzFetchGeneration();
	ports.invalidateAllAnlzCacheEntries();
	const wanted = new Map<string, DeckId[]>();
	for (const deck of deckIds) {
		const stableId = decks[deck].stable_id;
		if (stableId === null) continue;
		// One fetch per TRACK, not per deck: two decks loaded with the same
		// track would otherwise pull the same multi-MB payload twice.
		const holders = wanted.get(stableId);
		if (holders === undefined) wanted.set(stableId, [deck]);
		else holders.push(deck);
	}
	const staged = await Promise.all(
		[...wanted].map(async ([stableId, holders]) => {
			const [fresh, track] = await Promise.all([
				ports.fetchAnlzBypassingHttpCache(stableId),
				ports.fetchTrackBypassingHttpCache(stableId)
			]);
			return { stableId, holders, fresh, track };
		})
	);
	// Superseded while the fetches were in flight: the answer is discarded
	// below whatever it holds, so it must not be JUDGED first. On a loaded
	// runner the sibling /tracks/{id} fetch is served after a faster, later
	// switch has already reverted the daemon, and the cross-source guards
	// below then read that as a split and REJECT the superseded switch's
	// promise (setAnalysisSource) instead of letting it discard quietly - a
	// throw for a call whose result nobody was going to publish
	// (analysis-source.test.mjs "discards quietly instead of throwing",
	// nucbox-wsl-23, run 35731185371). The same check at the publish point
	// below stays: this one runs before the guards, that one after them.
	if (isSuperseded()) return null;
	const served = new Set(staged.map(({ fresh }) => fresh.beatgrid_source));
	if (served.size > 1) {
		throw new Error(
			`analysis source changed mid-refresh: /anlz served ${[...served].sort().join(' and ')} ` +
				'within one batch, so publishing would split the decks across both'
		);
	}
	// Two independent round trips: an external PUT to /api/v1/analysis/source
	// landing between them can serve each side of a switch even though
	// `served` agrees across tracks. `bpm` is beatgrid-lane-owned, so its
	// provenance source is the same selection `/anlz` stamped as
	// `beatgrid_source` - refuse to pair a grid and tempo that were never
	// measured together (discussion_r3972264411 P1 BLOCKING).
	for (const { stableId, fresh, track } of staged) {
		const bpmProvenance = track.provenance?.bpm;
		// A wholly ABSENT entry is the rekordbox side's genuine "no value" case
		// - apps.analysis.selection.effective_fields never synthesizes one for
		// an rbx lane with no track_fields row, so there is really nothing to
		// compare here. A PRESENT entry with a non-'ok' status is different: it
		// is always own-side (selection.py's own synthesized placeholder,
		// `source: "own-analysis"`, for a lane with no own record yet) - `status`
		// says whether the VALUE resolved, not which side produced the entry, so
		// skipping THIS case too let a row whose own analysis has not run yet
		// publish an own-stamped grid without ever comparing it against a stale
		// rekordbox-side answer from before a source switch (discussion_r3976638774
		// P2 BLOCKING).
		if (bpmProvenance === undefined) continue;
		const bpmOnOwn = _isOwnProvenanceSource(bpmProvenance.source);
		const gridOnOwn = fresh.beatgrid_source !== 'rekordbox';
		if (bpmOnOwn !== gridOnOwn) {
			throw new Error(
				`analysis source changed mid-refresh for ${stableId}: /anlz served beatgrid_source ` +
					`'${fresh.beatgrid_source}' but /tracks/{id} served bpm from '${bpmProvenance.source}' - ` +
					'the two parallel fetches landed on different sides of a source switch'
			);
		}
	}
	// The LAST point before anything below mutates a deck or the shared cache.
	// Every staged fetch above is an unbounded await, so a newer, faster
	// switch (or poll) can already have won and moved the mirror on while
	// this one was still in flight - publishing now would overwrite the
	// winner's decks/cache with this call's stale bytes, and no later poll
	// could ever detect the split because `deckFeatures` would still read as
	// the winner's value (discussion_r3975326238 P1 BLOCKING).
	if (isSuperseded()) return null;
	if (currentAnlzFetchGeneration() !== generationAtBatch) return null;
	// Merge each staged fetch onto whatever payload is CURRENTLY live for its
	// track, keeping only the fields this switch is actually authoritative
	// for: `beatgrid`, `beatgrid_source`, `beatgrid_own_unavailable_reason`,
	// `waveform`, and `phrases` (waveform varies with the waveform lane;
	// phrases must stay in sync with the server so an emptied PSSI list is
	// visible on a loaded deck).
	// A concurrent hot-cue write (`refreshHotCues`, audio-engine.svelte.ts:
	// 3053-3054) can publish newer cues/loop data onto a holder's deck.anlz
	// and the shared cache WHILE this staged `/anlz` fetch is still in
	// flight, since hot_cue_save/clear/restore claims `persistence-N`, not
	// the decks-plus-sync scope this refresh holds. Both requests share the
	// same post-switch generation and the same, correct beatgrid_source, so
	// neither the generation guard nor a wrong-source guard catches this -
	// it is a same-source, different-timing race, not a stale or
	// cross-source one. Reading the base as late as possible (here, not when
	// `fresh` was fetched) and overlaying only the source-owned fields mirrors
	// `adoptAuthoritativeGrid`'s own publish thunk (re-reads `deckAnlz(deck)`
	// and merges only `beatgrid` onto it, beatgrid-resync-guards.ts:358-366)
	// instead of letting this call's slower-settling fetch clobber a faster
	// cue write with `decks[deck].anlz = fresh`.
	const merged = staged.map(({ stableId, holders, fresh, track }) => {
		// The shared cache, not an arbitrary holder, is the freshest known base
		// for a track with more than one loaded deck (see `getReadyAnlz`'s own
		// docstring above for the race this closes). Only falls back to a
		// holder's own `.anlz` when the cache has nothing for this track yet.
		const liveHolder = holders.find((deck) => decks[deck].stable_id === stableId);
		const base = ports.getReadyAnlz(stableId) ?? (liveHolder === undefined ? null : decks[liveHolder].anlz);
		const anlz: AnlzData =
			base === null
				? fresh
				: {
						...base,
						beatgrid: fresh.beatgrid,
						beatgrid_source: fresh.beatgrid_source,
						beatgrid_own_unavailable_reason: fresh.beatgrid_own_unavailable_reason,
						waveform: fresh.waveform,
						phrases: fresh.phrases
					};
		return { stableId, holders, anlz, track };
	});
	// Collected, not awaited per-iteration (would serialize what the cache
	// write below deliberately doesn't); only this function's RETURN waits.
	// `alreadyScoped: true` below: this function already holds the claim,
	// so the sink reconciles inline, not via a deadlocking nested one (r3972154599).
	const sinkSettlements: Array<void | Promise<void>> = [];
	for (const { stableId, anlz } of merged) {
		sinkSettlements.push(ports.refreshAnlzCacheEntry(stableId, anlz, true));
		// `refreshAnlzCacheEntry` notifies the sink only for a payload that
		// HAS a grid, because the ambient prefetch it was built for can only
		// ever discover one. A deliberate switch can also REMOVE one: OWN with
		// no analysis record for this track serves the real empty grid plus
		// `beatgrid_own_unavailable_reason` (rb_assets.py). Left unannounced,
		// a playing Beat-Synced deck would just lose its grid - quantize and
		// phase-lock going inert mid-set with no `markSettledGridless`, no
		// follower abandonment and no sync error. `landed: false` is exactly
		// the settled-gridless case reconcileAfterBeatgridSettled already
		// handles. `anlz.beatgrid` is `fresh.beatgrid` unchanged by the merge
		// above, so this reads identically to checking `fresh` itself.
		if (!hasAnlzBeatgrid(anlz)) {
			sinkSettlements.push(ports.notifyGridlessSettlement(stableId, anlz, true));
		}
	}
	for (const { stableId, holders, anlz, track } of merged) {
		for (const deck of holders) {
			// A load() that landed mid-request owns this deck now; its own
			// fetch already ran under the post-switch generation.
			if (decks[deck].stable_id !== stableId) continue;
			decks[deck].anlz = anlz;
			// `?? null` for the same reason load() uses it: TrackOut spells
			// every nullable field optional, so absent and null both mean
			// "unknown" to a deck.
			decks[deck].bpm = track.bpm ?? null;
			decks[deck].key = track.key ?? null;
		}
	}
	await Promise.all(sinkSettlements);
	// `?? null` rather than a default: no loaded deck means nothing was served,
	// which is not the same claim as "the server served rekordbox".
	return [...served][0] ?? null;
}
