<script lang="ts">
	/**
	 * PLACEHOLDER for the ranked-candidates section, and deliberately inert.
	 *
	 * `BrowserPanel.svelte` has imported this component since cfbfe55
	 * ("wip(spike)", Fri 24 Jul 2026 01:19) but the file was never committed,
	 * so `/performance` failed to build for anyone without that author's
	 * working tree. This restores the build WITHOUT inventing the feature.
	 *
	 * The `candidates` funnel is unbuilt, and it renders NOTHING for it: the
	 * earlier inert "not implemented, N candidates available" marker
	 * was hidden for V1 (JIK, Thu 1 Oct 2026: hide unbuilt UI rather than
	 * show it inert; issue #876, merging NEXT and RECOMMENDED into one rail,
	 * stays open). The props below are exactly the ones BrowserPanel already
	 * passes, so replacing this file with the real implementation needs no
	 * change at the call site.
	 *
	 * TODO(owner of cfbfe55): replace with the real recommended-tracks section.
	 *
	 * Pin 72ac80073f92 (issue #878), the maintainer: "lv1 pairing completion would be
	 * very easy. And we could default show paired track in recommended bar
	 * for now? Not used otherwise atm." The candidates funnel above is still
	 * unbuilt, but LV1 pairing (POST /api/v1/pairings, already live) is real
	 * data, so this section ALSO shows any existing pairing for `stableId`
	 * (the deck-1-loaded track) by default - a second, independent row, not
	 * a candidate. "Not used otherwise atm" per the pin: this is the only
	 * consumer of pairings on /performance today.
	 *
	 * FAIL-FAST NOTE (bot review, PR #1285): the first cut of this caught
	 * every failure - listPairingsFor, per-partner getTrack - and rendered
	 * the same empty/placeholder state as "this track genuinely has no
	 * pairings". That is exactly the ambiguous-failure shape the repo's
	 * fail-fast rule exists to prevent: a 401, an outage or a contract
	 * break was indistinguishable from a normal empty result. Fixed with
	 * the same explicit-state discriminated union SuggestNextStrip.svelte
	 * already established (idle/loading/loaded/error, ApiError imported
	 * from $lib/api/client) rather than a generic toast painted over an
	 * empty list underneath:
	 *   - a listPairingsFor failure renders a visible `.rec-error` row,
	 *     never a silently-empty section;
	 *   - a getTrack failure is split on status: a confirmed 404 (the
	 *     partner really was deleted) still shows the raw partner id, same
	 *     as before; any OTHER status (401, 500, network) is flagged
	 *     per-row as `trackLoadFailed` and rendered as a visible error
	 *     chip instead of silently falling back to the raw-id row, so a
	 *     transient/auth failure never masquerades as "partner deleted".
	 */
	import { listPairingsFor, getTrack, type Pairing, type Track } from '$lib/api';
	import { ApiError } from '$lib/api/client';
	import { subscribeKind, subscribeResync } from '$lib/api/events-bus';

	type Props = {
		candidates?: unknown[];
		currentPlaylistId?: string | null;
		currentPlaylistMemberIds?: Set<string> | string[] | null;
		referenceBpm?: number | null;
		referenceKey?: string | null;
		/** Deck-1-loaded track, same convention as SuggestNextStrip's `stableId`. */
		stableId?: string | null;
		onload?: (stableId: string) => void;
		/** Load a candidate and start it playing. pressT0Ms is the triggering
		 * double-click's own event.timeStamp, Q1's operator-felt press stamp. */
		onplay?: (stableId: string, pressT0Ms: number) => void;
		onhover?: (stableId: string) => void;
	};

	let { stableId = null, onload, onplay, onhover }: Props = $props();

	type PairedTrack = {
		pairing: Pairing;
		partnerId: string;
		track: Track | null;
		/** True only for a genuine getTrack failure (not a confirmed 404) -
		 * rendered as a visible error chip, never silently as "deleted". */
		trackLoadFailed: boolean;
	};

	let pairedTracks = $state<PairedTrack[]>([]);
	/** Non-null iff listPairingsFor itself failed - distinct from `pairedTracks`
	 * being empty, which means the call succeeded and found nothing. */
	let loadError = $state<string | null>(null);
	let requestSeq = 0; // stale-response guard, same pattern as SuggestNextStrip
	/** Bumped when the server says pairings changed (a Capture from the
	 * Create pairing sheet, a delete on /pairings), so a new pairing for the
	 * loaded track shows without reloading the deck. */
	let pairingsRevision = $state(0);
	let shownFor: string | null = null;

	$effect(() => {
		const unkind = subscribeKind('pairings', () => {
			pairingsRevision += 1;
		});
		const unresync = subscribeResync(() => {
			pairingsRevision += 1;
		});
		return () => {
			unkind();
			unresync();
		};
	});

	function _partnerId(p: Pairing, sid: string): string {
		return p.from_stable_id === sid ? p.to_stable_id : p.from_stable_id;
	}

	async function _resolvePartner(
		pairing: Pairing,
		sid: string
	): Promise<PairedTrack> {
		const partnerId = _partnerId(pairing, sid);
		try {
			const track = (await getTrack(partnerId)).track;
			return { pairing, partnerId, track, trackLoadFailed: false };
		} catch (e) {
			if (e instanceof ApiError && e.status === 404) {
				// Confirmed gone, not a failure: still show the raw id.
				return { pairing, partnerId, track: null, trackLoadFailed: false };
			}
			// Any other status (401, 500, network) is a real failure - do
			// not paint it the same as a confirmed deletion.
			return { pairing, partnerId, track: null, trackLoadFailed: true };
		}
	}

	$effect(() => {
		const sid = stableId;
		void pairingsRevision;
		const seq = ++requestSeq;
		const sameTrack = sid === shownFor;
		shownFor = sid;
		if (sid === null) {
			pairedTracks = [];
			loadError = null;
			return;
		}
		loadError = null;
		// Clear synchronously, before the async call resolves: otherwise the
		// PREVIOUS stableId's paired rows stay clickable while a new request
		// is in flight, and a click there loads/plays the wrong track (P2,
		// bot review PR #1285). A blank section for one request's duration
		// is the honest state - there is nothing yet confirmed true for the
		// new stableId. A refresh for the SAME track keeps its rows until the
		// answer lands: they are still true for this track.
		if (!sameTrack) pairedTracks = [];
		listPairingsFor(sid)
			.then(async (pairings) => {
				if (seq !== requestSeq) return;
				const withTracks = await Promise.all(
					pairings.map((pairing) => _resolvePartner(pairing, sid))
				);
				if (seq !== requestSeq) return;
				pairedTracks = withTracks;
			})
			.catch((e) => {
				if (seq !== requestSeq) return;
				pairedTracks = [];
				loadError = e instanceof Error ? e.message : String(e);
			});
	});
</script>

{#if loadError !== null}
	<div class="rec-error" role="alert" title={loadError}>
		Could not load paired track: {loadError}
	</div>
{:else if pairedTracks.length > 0}
	<div class="rec-paired" role="group" aria-label="paired tracks">
		{#each pairedTracks as p (p.pairing.pairing_id)}
			{#if p.trackLoadFailed}
				<div
					class="rec-paired-row rec-paired-row-failed"
					role="alert"
					title={`Paired ${p.pairing.direction} ${p.partnerId} - could not load this track's details`}
				>
					<span class="tag tag-error">PAIRED</span>
					<span class="title">Could not load track {p.partnerId}</span>
				</div>
			{:else}
				<button
					type="button"
					class="rec-paired-row"
					title={`Paired ${p.pairing.direction} ${p.track?.title ?? p.partnerId}${p.pairing.notes ? ` - ${p.pairing.notes}` : ''}`}
					onpointerenter={() => onhover?.(p.partnerId)}
					onpointerleave={() => onhover?.('')}
					onclick={() => onload?.(p.partnerId)}
					ondblclick={(e) => onplay?.(p.partnerId, e.timeStamp)}
				>
					<span class="tag">PAIRED</span>
					<span class="title">{p.track?.title ?? p.partnerId}</span>
					{#if p.track?.artist}<span class="artist">{p.track.artist}</span>{/if}
				</button>
			{/if}
		{/each}
	</div>
{/if}

<style>
	.rec-error {
		padding: 0.4rem 0.6rem;
		font-size: 0.78rem;
		color: var(--danger, #d9534f);
		border: 1px solid var(--danger, #d9534f);
		border-radius: 6px;
		margin: 0.4rem 0;
		cursor: help;
	}
	.rec-paired {
		display: flex;
		flex-direction: column;
		gap: 2px;
		margin: 0.4rem 0;
	}
	.rec-paired-row-failed {
		color: var(--danger, #d9534f);
		border-color: var(--danger, #d9534f);
		cursor: help;
	}
	.rec-paired-row .tag-error {
		background: var(--danger, #d9534f);
	}
	.rec-paired-row {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 0.3rem 0.6rem;
		font-size: 0.78rem;
		color: var(--rb-text, #c8cdd2);
		background: transparent;
		border: 1px solid var(--border, #1c222c);
		border-radius: 6px;
		cursor: pointer;
		text-align: left;
		width: 100%;
	}
	.rec-paired-row:hover {
		border-color: var(--rb-accent, #3d7dd9);
	}
	.rec-paired-row .tag {
		font-size: 9px;
		padding: 0 4px;
		border-radius: 2px;
		background: var(--rb-accent, #3d7dd9);
		color: #fff;
		flex: none;
	}
	.rec-paired-row .title {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.rec-paired-row .artist {
		color: var(--muted, #9aa4b2);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		flex: none;
		max-width: 40%;
	}
</style>
