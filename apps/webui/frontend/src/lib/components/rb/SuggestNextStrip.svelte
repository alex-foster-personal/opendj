<script module lang="ts">
	/**
	 * One published candidate. `rating` is always null: POST
	 * /copilot/suggest-next returns CopilotTrackOut, which has no rating
	 * field, so there is no honest value to put here. It stays in the shape
	 * because the consuming Recommended grouping asks for it, and null is
	 * the accurate "the suggest-next contract does not carry this" answer
	 * rather than a fabricated number.
	 */
	export interface SuggestCandidate {
		stable_id: string;
		title: string | null;
		artist: string | null;
		bpm: number | null;
		key_camelot: string | null;
		energy: number | null;
		rating: number | null;
		rationale_tags: string[];
		explain_text: string | null;
	}
</script>

<script lang="ts">
	// SuggestNextStrip -- renders POST /api/v1/copilot/suggest-next for the
	// deck-1-loaded track (gating-wave unit: dj_copilot router).
	//
	// NOT MOUNTED HERE. Recommended one-line mount (integrator, BrowserPanel
	// bottom or below the deck-1 column in +page):
	//   <SuggestNextStrip stableId={deck1LoadedStableId} />
	//
	// Contract (apps/webui/server/routes/copilot.py docstring is canonical):
	//   200 -> {current, context_source, context_size, candidates[]}
	//   404 -> {error: 'not_found', message}
	//   422 -> {error: 'insufficient_data', message,
	//           details: {stable_id, missing: {bpm[], key[], energy[]}}}
	// Five explicit UI states, none invented: idle (no deck-1 track),
	// loading, insufficient-data (names the missing fields), error, and
	// loaded (candidate chips OR an explicit "no compatible tracks" empty).
	//
	// CONVERTED onto the generated OpenAPI client (src/lib/api/client.ts),
	// in place per the conversion pattern: transport only, the wire
	// interfaces, StripState machine and the $effect/requestSeq guard are
	// untouched. The copilot router answers errors as TOP-LEVEL
	// {error, message, ...} bodies (ErrorBody, not the detail envelope),
	// so the mapping below reads them off ApiError.body.
	import { ApiError, api, unwrap } from '$lib/api/client';
	import { DECK_IDS } from '$lib/player/constants';
	import { getDeckState } from '$lib/player/state.svelte';
	import { pushPlayed } from '$lib/rb/peak-play-timeline';
	import { isUsbTrackId, withoutUsbTrackIds } from '$lib/rb/track-source';
	import { visibleRationaleTags } from '$lib/rb/suggest-tags';
	import ControlExplainer from './deck/ControlExplainer.svelte';
	import PeakPressureCoach from './PeakPressureCoach.svelte';

	// Pin 946e04da2d0d: several of these chevrons sit edge to edge, so a fast
	// pointer skim across the strip would otherwise flash a popover per tile.
	// 50ms is enough to swallow a skim without being felt as lag on a
	// deliberate hover (the existing keystroke-debounce convention in this
	// codebase - see LOCAL_FILTER_DEBOUNCE_MS - runs the same 60-100ms band).
	const CHEVRON_EXPLAIN_DELAY_MS = 50;

	interface SuggestionWire {
		stable_id: string;
		title: string | null;
		artist: string | null;
		bpm: number | null;
		key_camelot: string | null;
		energy: number | null;
		score: number;
		rationale_tags: string[];
		rationale_numbers: Record<string, number>;
		explain_text: string | null;
	}
	interface SuggestNextWire {
		current: Omit<SuggestionWire, 'score' | 'rationale_tags' | 'rationale_numbers' | 'explain_text'>;
		context_source: string;
		context_size: number;
		candidates: SuggestionWire[];
		pressure: {
			score: number;
			cue: 'keep_building' | 'hold' | 'release' | 'unknown';
			scored_tracks: number;
			skipped_unknown: number;
			cue_label: string;
			advisory: string;
			limitation: string;
		};
	}
	interface InsufficientWire {
		error: string;
		message: string;
		details: { stable_id: string; missing: Record<string, string[]> } | null;
	}

	type StripState =
		| { kind: 'idle' }
		| { kind: 'stick' }
		| { kind: 'loading' }
		| { kind: 'loaded'; data: SuggestNextWire }
		| { kind: 'insufficient'; missing: string[]; message: string }
		| { kind: 'error'; message: string };

	let {
		stableId,
		sessionIds = [],
		topN = 8,
		targetLabel,
		playTargetLabel,
		onload,
		onplay,
		onhover,
		oncandidates
	}: {
		stableId: string | null;
		sessionIds?: string[];
		topN?: number;
		/** Owner-formatted destination, or null when that action is unavailable. */
		targetLabel: string | null;
		playTargetLabel: string | null;
		/** Click handling revalidates deck availability at execution time. */
		onload?: (stableId: string) => void;
		/** Load a candidate and start it playing. pressT0Ms is the triggering
		 * click's own event.timeStamp, Q1's operator-felt press stamp. */
		onplay?: (stableId: string, pressT0Ms: number) => void;
		/** Pointer entered a candidate, or null when it left. */
		onhover?: (stableId: string | null) => void;
		/** Republish the ranked candidates whenever a fetch settles. */
		oncandidates?: (candidates: SuggestCandidate[]) => void;
	} = $props();

	function _toCandidates(data: SuggestNextWire): SuggestCandidate[] {
		return data.candidates.map((c) => ({
			stable_id: c.stable_id,
			title: c.title,
			artist: c.artist,
			bpm: c.bpm,
			key_camelot: c.key_camelot,
			energy: c.energy,
			rating: null,
			rationale_tags: [...c.rationale_tags],
			explain_text: c.explain_text
		}));
	}

	let stripState: StripState = $state({ kind: 'idle' });
	let requestSeq = 0; // stale-response guard
	let localSessionIds: string[] = $state([]);
	const prevDeckIds: Record<number, string | null> = {};

	$effect(() => {
		if (sessionIds.length > 0) return;
		for (const deckId of DECK_IDS) {
			const currentId = getDeckState(deckId).stable_id;
			const previousId = prevDeckIds[deckId] ?? null;
			if (previousId !== null && currentId !== previousId) {
				localSessionIds = pushPlayed(localSessionIds, previousId);
			}
			prevDeckIds[deckId] = currentId;
		}
	});

	$effect(() => {
		const sid = stableId;
		// Spec 4b: stick ids never reach the copilot, which 404s on ids it does
		// not know - one in the session would fail suggestions for library
		// tracks too, and a stick track on deck 1 gets none at all.
		const session = withoutUsbTrackIds(sessionIds.length > 0 ? sessionIds : localSessionIds);
		const seq = ++requestSeq;
		if (sid === null || isUsbTrackId(sid)) {
			stripState = { kind: sid === null ? 'idle' : 'stick' };
			oncandidates?.([]);
			return;
		}
		stripState = { kind: 'loading' };
		_fetchSuggestions(sid, session).then((next) => {
			if (seq !== requestSeq) return;
			stripState = next;
			// Republish on every settled outcome, so a failed or
			// insufficient-data fetch clears the previous track's
			// candidates instead of leaving them on screen as if current.
			oncandidates?.(next.kind === 'loaded' ? _toCandidates(next.data) : []);
		});
	});

	// ----------------------------------------------------------- _helpers

	async function _fetchSuggestions(sid: string, session: string[]): Promise<StripState> {
		try {
			// explain: false is the server default the old raw fetch relied on;
			// the generated SuggestNextIn requires the field, so it is explicit.
			const data = await unwrap(
				api.POST('/api/v1/copilot/suggest-next', {
					body: { stable_id: sid, session_ids: session, top_n: topN, explain: false }
				})
			);
			return { kind: 'loaded', data: data as unknown as SuggestNextWire };
		} catch (e) {
			if (e instanceof ApiError && e.status === 422) {
				const body = e.body as InsufficientWire | null;
				const missing = Object.entries(body?.details?.missing ?? {})
					.filter(([, ids]) => ids.length > 0)
					.map(([fieldName]) => fieldName);
				return { kind: 'insufficient', missing, message: body?.message ?? e.message };
			} else if (e instanceof ApiError) {
				const body = e.body as { error?: string; message?: string } | null;
				return {
					kind: 'error',
					message: `${body?.error ?? `HTTP_${e.status}`}: ${body?.message ?? e.message}`
				};
			}
			return { kind: 'error', message: e instanceof Error ? e.message : String(e) };
		}
	}

	function _tagLabel(tag: string): string {
		return tag.replaceAll('_', ' ');
	}

	function _loadControlLabel(candidate: SuggestionWire, play: boolean): string {
		const track = candidate.title ?? candidate.stable_id;
		const action = play ? 'Load and play' : 'Load';
		const destination = (play ? playTargetLabel : targetLabel) ?? 'no free deck available';
		const explanation = candidate.explain_text === null ? '' : ` - ${candidate.explain_text}`;
		return `${action} ${track} to ${destination}${explanation}`;
	}

	/**
	 * Pin 946e04da2d0d: the chevron's bare native title only named the
	 * destination deck. This spells out the actual effect of the click
	 * (loads AND starts playback immediately, distinct from the tile button
	 * beside it which only loads), plus the rationale when the server gave one.
	 */
	function _chevronExplain(candidate: SuggestionWire): { title: string; bullets: string[] } {
		const track = candidate.title ?? candidate.stable_id;
		const destination = playTargetLabel ?? 'no free deck available';
		const bullets = [`Loads ${track} onto ${destination} and starts playback immediately.`];
		if (candidate.explain_text !== null) bullets.push(candidate.explain_text);
		return { title: `Load and play to ${destination}`, bullets };
	}
</script>

<section class="strip" aria-label="suggested next tracks">
	<span class="head">NEXT</span>
	{#if stripState.kind === 'loaded' && stripState.data.pressure.cue !== 'unknown'}
		<PeakPressureCoach cue={stripState.data.pressure.cue} cueLabel={stripState.data.pressure.cue_label} />
	{/if}
	{#if stripState.kind === 'idle'}
		<span class="dim">load a track on deck 1 for suggestions</span>
	{:else if stripState.kind === 'stick'}
		<span class="dim" title="Suggestions rank your library; a track playing from a USB stick is not in it">no suggestions for a USB stick track</span>
	{:else if stripState.kind === 'loading'}
		<span class="dim">ranking candidates…</span>
	{:else if stripState.kind === 'insufficient'}
		<span class="warn" title={stripState.message}>
			track not analyzed: missing {stripState.missing.join(', ')}
		</span>
	{:else if stripState.kind === 'error'}
		<span class="err" role="alert">suggest-next failed: {stripState.message}</span>
	{:else if stripState.data.candidates.length === 0}
		<span class="dim">no compatible tracks in library for this BPM/key window</span>
	{:else}
		<ol class="cands">
			{#each stripState.data.candidates as cand (cand.stable_id)}
				{@const chevronExplain = _chevronExplain(cand)}
				<li
					class="cand"
					onpointerenter={() => onhover?.(cand.stable_id)}
					onpointerleave={() => onhover?.(null)}
				>
					<button
						type="button"
						class="cand-btn"
						data-stable-id={cand.stable_id}
						title={_loadControlLabel(cand, false)}
						aria-label={_loadControlLabel(cand, false)}
						disabled={targetLabel === null}
						onclick={() => onload?.(cand.stable_id)}
					>
						<span class="title">
							{cand.title ?? cand.stable_id}
							{#if cand.artist}· {cand.artist}{/if}
							{#if cand.energy !== null}· E{cand.energy}{/if}
						</span>
						<!-- LIBUX-03: the bpm/camelot readouts lived in a dedicated
						     .meta row; a trimmed-but-present row still costs a full
						     line of height, so the row is gone, not just its text.
						     Artist/energy moved onto the title line.

						     Tags below: only ones the line above does not already
						     say. bpm / camelot / energy are redundant there too
						     (pin 407a1601defe), so they cost a line for nothing.
						     The span itself is gone when nothing survives, which is
						     where the vertical space comes back. -->
						{#if visibleRationaleTags(cand.rationale_tags).length > 0}
							<span class="tags">
								{#each visibleRationaleTags(cand.rationale_tags) as tag (tag)}
									<span class="tag" class:pair={tag.startsWith('pair_')}>{_tagLabel(tag)}</span>
								{/each}
							</span>
						{/if}
					</button>
					<!-- Pin dd4f0f5ae33f: this button had both a native `title` and,
					     now it has ControlExplainer's rich popover, would show BOTH
					     on hover - the OS tooltip stacked on top of the custom one,
					     which no z-index can fix (native title is always OS-topmost).
					     The fix is not having two tooltip mechanisms live at once:
					     drop the native title, keep aria-label for accessibility. -->
					<ControlExplainer
						title={chevronExplain.title}
						bullets={chevronExplain.bullets}
						showDelayMs={CHEVRON_EXPLAIN_DELAY_MS}
					>
						<button
							type="button"
							class="play-btn"
							data-stable-id={cand.stable_id}
							aria-label={_loadControlLabel(cand, true)}
							disabled={playTargetLabel === null}
							onclick={(e) => onplay?.(cand.stable_id, e.timeStamp)}
						>
							<svg viewBox="0 0 8 10" width="8" height="10" aria-hidden="true">
								<path d="M1 1 L7 5 L1 9 Z" fill="currentColor" />
							</svg>
						</button>
					</ControlExplainer>
				</li>
			{/each}
		</ol>
	{/if}
</section>

<style>
	.strip {
		display: flex;
		align-items: center;
		gap: 8px;
		min-height: 26px;
		padding: 2px 8px;
		background: var(--rb-panel);
		border-top: 1px solid var(--rb-border);
		font-family: var(--rb-font);
		font-size: var(--rb-fs-browser);
		color: var(--rb-text);
		overflow-x: auto;
	}
	.head {
		flex: none;
		font-size: var(--rb-fs-label);
		letter-spacing: 0.08em;
		color: var(--rb-accent);
	}
	.dim {
		color: var(--rb-text-dim);
	}
	.warn {
		color: var(--rb-yellow);
	}
	.err {
		color: var(--rb-red);
	}
	.cands {
		display: flex;
		gap: 6px;
		margin: 0;
		padding: 0;
		list-style: none;
	}
	/* Pin 946e04da2d0d: the tile-button + chevron used to be two separately
	   bordered chips. The shared border/background/radius now live here, on
	   the wrapping group, so the two read as one grouped control ("button
	   not clearly attached to the tile to the left") and one hover/focus
	   state highlights both halves together. `position: relative` plus the
	   hover/focus z-index bump below is belt-and-braces stacking safety on
	   top of the min-width:0 fix (next rule): a hovered/focused tile always
	   paints above its neighbors in the strip, so it can never end up hidden
	   under the next NEXT tile, even if future content changes the math. */
	.cand {
		position: relative;
		display: flex;
		align-items: stretch;
		gap: 0;
		flex: none;
		max-width: 180px;
		list-style: none;
		border: 1px solid var(--rb-border);
		border-radius: 3px;
		background: var(--rb-panel-raised);
	}
	.cand:hover,
	.cand:focus-within {
		z-index: 1;
		border-color: var(--rb-accent);
	}
	/* The chevron's ControlExplainer wrapper sits between .cand and .play-btn
	   in the DOM; make it a transparent, non-growing flex passthrough so it
	   does not change the group's layout. */
	.cand :global(.explainer) {
		display: flex;
		flex: none;
		align-self: stretch;
	}
	.play-btn {
		display: flex;
		flex: none;
		align-items: center;
		align-self: stretch;
		padding: 0 4px;
		background: transparent;
		border: none;
		border-left: 1px solid var(--rb-border);
		color: var(--rb-text-dim);
		cursor: pointer;
	}
	.cand:hover .play-btn,
	.cand:focus-within .play-btn {
		color: var(--rb-accent);
	}
	.cand-btn {
		display: flex;
		flex-direction: column;
		gap: 1px;
		width: 100%;
		min-width: 0;
		padding: 2px 6px;
		background: transparent;
		border: none;
		border-radius: 3px;
		color: inherit;
		font: inherit;
		text-align: left;
		cursor: pointer;
	}
	.cand:hover .cand-btn,
	.cand:focus-within .cand-btn {
		background: color-mix(in srgb, var(--rb-accent) 14%, var(--rb-panel-raised));
	}
	.title {
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.tags {
		display: flex;
		gap: 3px;
		flex-wrap: wrap;
	}
	.tag {
		font-size: 9px;
		padding: 0 3px;
		border-radius: 2px;
		background: var(--rb-select);
		color: var(--rb-wave-high);
	}
	.tag.pair {
		background: var(--rb-green);
		color: var(--rb-bg);
	}
</style>
