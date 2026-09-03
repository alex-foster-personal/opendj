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
	import { visibleRationaleTags } from '$lib/rb/suggest-tags';

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
	}
	interface InsufficientWire {
		error: string;
		message: string;
		details: { stable_id: string; missing: Record<string, string[]> } | null;
	}

	type StripState =
		| { kind: 'idle' }
		| { kind: 'loading' }
		| { kind: 'loaded'; data: SuggestNextWire }
		| { kind: 'insufficient'; missing: string[]; message: string }
		| { kind: 'error'; message: string };

	let {
		stableId,
		sessionIds = [],
		topN = 8,
		onload,
		onplay,
		onhover,
		oncandidates
	}: {
		stableId: string | null;
		sessionIds?: string[];
		topN?: number;
		/** Click a candidate to load (deck null = free deck). */
		onload?: (stableId: string) => void;
		/** Load a candidate and start it playing. */
		onplay?: (stableId: string) => void;
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

	let state: StripState = $state({ kind: 'idle' });
	let requestSeq = 0; // stale-response guard

	$effect(() => {
		const sid = stableId;
		const session = [...sessionIds];
		const seq = ++requestSeq;
		if (sid === null) {
			state = { kind: 'idle' };
			oncandidates?.([]);
			return;
		}
		state = { kind: 'loading' };
		_fetchSuggestions(sid, session).then((next) => {
			if (seq !== requestSeq) return;
			state = next;
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
</script>

<section class="strip" aria-label="suggested next tracks">
	<span class="head">NEXT</span>
	{#if state.kind === 'idle'}
		<span class="dim">load a track on deck 1 for suggestions</span>
	{:else if state.kind === 'loading'}
		<span class="dim">ranking candidates…</span>
	{:else if state.kind === 'insufficient'}
		<span class="warn" title={state.message}>
			track not analyzed: missing {state.missing.join(', ')}
		</span>
	{:else if state.kind === 'error'}
		<span class="err" role="alert">suggest-next failed: {state.message}</span>
	{:else if state.data.candidates.length === 0}
		<span class="dim">no compatible tracks in library for this BPM/key window</span>
	{:else}
		<ol class="cands">
			{#each state.data.candidates as cand (cand.stable_id)}
				<li
					class="cand"
					onpointerenter={() => onhover?.(cand.stable_id)}
					onpointerleave={() => onhover?.(null)}
				>
					<button
						type="button"
						class="cand-btn"
						data-stable-id={cand.stable_id}
						title={cand.explain_text ?? `Load ${cand.title ?? cand.stable_id}`}
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
					<button
						type="button"
						class="play-btn"
						data-stable-id={cand.stable_id}
						title={`Load and play ${cand.title ?? cand.stable_id}`}
						aria-label={`load and play ${cand.title ?? cand.stable_id}`}
						onclick={() => onplay?.(cand.stable_id)}
					>
						<svg viewBox="0 0 8 10" width="8" height="10" aria-hidden="true">
							<path d="M1 1 L7 5 L1 9 Z" fill="currentColor" />
						</svg>
					</button>
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
	.cand {
		display: flex;
		align-items: stretch;
		gap: 2px;
		flex: none;
		max-width: 180px;
		list-style: none;
	}
	.play-btn {
		display: flex;
		flex: none;
		align-items: center;
		padding: 0 4px;
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		border-radius: 3px;
		color: var(--rb-text-dim);
		cursor: pointer;
	}
	.play-btn:hover {
		border-color: var(--rb-accent);
		color: var(--rb-accent);
	}
	.cand-btn {
		display: flex;
		flex-direction: column;
		gap: 1px;
		width: 100%;
		padding: 2px 6px;
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		border-radius: 3px;
		color: inherit;
		font: inherit;
		text-align: left;
		cursor: pointer;
	}
	.cand-btn:hover {
		border-color: var(--rb-accent);
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
