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
	import { RB_API_BASE } from '$lib/rb/api-rb';

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
		onload
	}: {
		stableId: string | null;
		sessionIds?: string[];
		topN?: number;
		/** Click a candidate to load (deck null = free deck). */
		onload?: (stableId: string) => void;
	} = $props();

	let state: StripState = $state({ kind: 'idle' });
	let requestSeq = 0; // stale-response guard

	$effect(() => {
		const sid = stableId;
		const session = [...sessionIds];
		const seq = ++requestSeq;
		if (sid === null) {
			state = { kind: 'idle' };
			return;
		}
		state = { kind: 'loading' };
		_fetchSuggestions(sid, session).then((next) => {
			if (seq === requestSeq) state = next;
		});
	});

	// ----------------------------------------------------------- _helpers

	async function _fetchSuggestions(sid: string, session: string[]): Promise<StripState> {
		try {
			const r = await fetch(`${RB_API_BASE}/api/v1/copilot/suggest-next`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
				body: JSON.stringify({ stable_id: sid, session_ids: session, top_n: topN })
			});
			if (r.status === 422) {
				const body = (await r.json()) as InsufficientWire;
				const missing = Object.entries(body.details?.missing ?? {})
					.filter(([, ids]) => ids.length > 0)
					.map(([fieldName]) => fieldName);
				return { kind: 'insufficient', missing, message: body.message };
			} else if (!r.ok) {
				const body = (await r.json()) as { error?: string; message?: string };
				return {
					kind: 'error',
					message: `${body.error ?? `HTTP_${r.status}`}: ${body.message ?? r.statusText}`
				};
			}
			return { kind: 'loaded', data: (await r.json()) as SuggestNextWire };
		} catch (e) {
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
				<li class="cand">
					<button
						type="button"
						class="cand-btn"
						data-stable-id={cand.stable_id}
						title={cand.explain_text ?? `Load ${cand.title ?? cand.stable_id}`}
						onclick={() => onload?.(cand.stable_id)}
					>
						<span class="title">{cand.title ?? cand.stable_id}</span>
						<span class="meta">
							{cand.artist ?? '?'}
							· {cand.bpm === null ? '?' : cand.bpm.toFixed(1)}
							· {cand.key_camelot ?? '?'}
							{#if cand.energy !== null}· E{cand.energy}{/if}
						</span>
						<span class="tags">
							{#each cand.rationale_tags as tag (tag)}
								<span class="tag" class:pair={tag.startsWith('pair_')}>{_tagLabel(tag)}</span>
							{/each}
						</span>
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
		flex: none;
		max-width: 180px;
		list-style: none;
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
	.meta {
		color: var(--rb-text-dim);
		font-size: var(--rb-fs-label);
		white-space: nowrap;
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
