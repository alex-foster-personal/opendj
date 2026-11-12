<script lang="ts">
	/**
	 * BACKSTAGE-08: the Backstage "Human review" widget (/admin?p=triage, flag
	 * shell.backstage on). One item of the REAL triage queue at a time
	 * (GET /api/v1/lyrics?order=suspect, the listing LyricTriage reads), with
	 * that track's most suspect line from GET /tracks/{id}/lyrics/words.
	 *
	 * Labels: there is no write endpoint for Correct / Wrong words / Wrong
	 * timing / Not this song, so the four buttons (and keys 1-4) are inert
	 * with the PARITY-TODO tooltip. Nothing is written to a mock or to
	 * browser storage; 0 skips (session only), W/S or Up/Down step.
	 *
	 * Cue line: the library preview player (CUEOUT-15), which connects only to
	 * the headphone cue bus and never loads or touches a deck. It is imported
	 * lazily on click so /admin does not pull the audio engine at page load.
	 */
	import { onMount } from 'svelte';
	import { getTrack, type LyricVerdict } from '$lib/api';
	import { getTrackLyricsWords } from '$lib/api-karaoke';
	import { NOT_IMPLEMENTED_TITLE as NOT_IMPLEMENTED, nextNavIndex } from '$lib/shell/backstage-nav';
	import { fetchLyricTriage } from './lyrics-api';
	import { tip } from './tooltip.svelte';
	import './admin-state.css';
	import {
		LABELS_WRITABLE,
		REVIEW_LABELS,
		advanceReview,
		fmtTime,
		pickSuspectLine,
		positionText,
		reviewKeyAction,
		type SuspectLine
	} from './review-queue';

	const LIMIT = 100;

	let rows = $state<LyricVerdict[]>([]);
	let loaded = $state(false);
	let error = $state<string | null>(null);
	let index = $state(0);
	let skipped = $state<Set<string>>(new Set());
	let notice = $state<string | null>(null);

	type LineState =
		| { kind: 'loading' }
		| { kind: 'ok'; line: SuspectLine | null; missing: string | null }
		| { kind: 'err'; message: string };
	let lineState = $state<LineState>({ kind: 'loading' });
	let cueBusy = $state(false);

	const item = $derived(rows[index] ?? null);

	onMount(async () => {
		try {
			rows = await fetchLyricTriage({ order: 'suspect', limit: LIMIT });
			loaded = true;
		} catch (exc) {
			error = exc instanceof Error ? exc.message : String(exc);
		}
	});

	let lineGen = 0;
	$effect(() => {
		const sid = item?.stable_id;
		if (!sid) return;
		const gen = ++lineGen;
		lineState = { kind: 'loading' };
		let missing: string | null = null;
		getTrackLyricsWords(sid, {
			includeLines: true,
			onMissing: (detail) => (missing = detail)
		}).then(
			(track) => {
				if (gen !== lineGen) return;
				lineState = {
					kind: 'ok',
					line: track ? pickSuspectLine(track.words, track.lines) : null,
					missing
				};
			},
			(exc) => {
				if (gen !== lineGen) return;
				lineState = { kind: 'err', message: exc instanceof Error ? exc.message : String(exc) };
			}
		);
	});

	// Window-level, so 1-4 / 0 / W-S work as soon as the page lands; this
	// component is mounted only while the review page is shown, so the
	// listener lives exactly that long. A key another handler consumed
	// (the Backstage nav) is left alone.
	function onKeydown(event: KeyboardEvent): void {
		if (event.defaultPrevented) return;
		const action = reviewKeyAction(event, event.target);
		if (action === null || rows.length === 0) return;
		event.preventDefault();
		notice = null;
		if (action.kind === 'step') {
			index = nextNavIndex(index, rows.length, action.arrow, action.modified);
		} else if (action.kind === 'skip') {
			if (item) skipped = new Set(skipped).add(item.stable_id);
			index = advanceReview(index, rows.length);
		} else if (!LABELS_WRITABLE) {
			notice = `Label "${REVIEW_LABELS.find((l) => l.id === action.label)?.text}": ${NOT_IMPLEMENTED}`;
		}
	}

	async function cueLine(): Promise<void> {
		const it = item;
		if (!it || lineState.kind !== 'ok' || lineState.line === null || cueBusy) return;
		const line = lineState.line;
		if (line.start_s === null) {
			notice = 'Cue line: this line has no start time';
			return;
		}
		cueBusy = true;
		notice = null;
		try {
			const { track } = await getTrack(it.stable_id);
			const durMs = track.duration_ms ?? null;
			if (!durMs || durMs <= 0) {
				notice = 'Cue line: the track has no known duration';
				return;
			}
			const cue = await import('$lib/player/preview-cue.svelte');
			const outcome = await cue.previewCueSeek(it.stable_id, (line.start_s * 1000) / durMs);
			if (!outcome.ok) {
				if (outcome.reason !== cue.PREVIEW_SUPERSEDED) notice = outcome.reason;
				return;
			}
			if (outcome.warning) notice = outcome.warning;
			const lenMs =
				line.end_s !== null ? Math.max(500, (line.end_s - line.start_s) * 1000 + 300) : 4000;
			const sid = it.stable_id;
			setTimeout(() => {
				if (cue.previewCue.stable_id === sid && cue.previewCue.playing) cue.stopPreviewCue();
			}, lenMs);
		} catch (exc) {
			notice = `Cue line failed: ${exc instanceof Error ? exc.message : String(exc)}`;
		} finally {
			cueBusy = false;
		}
	}

	const pct = $derived(rows.length ? Math.round(((index + 1) / rows.length) * 100) : 0);
</script>

<svelte:window onkeydown={onKeydown} />

<section class="review-panel admin-st" data-testid="backstage-review">
	<h3>Human review</h3>
	{#if error}
		<div class="error">
			LOAD FAILED

			{error}
		</div>
	{:else if !loaded}
		<p class="copy">Loading review queue...</p>
	{:else if rows.length === 0}
		<p class="copy" data-testid="review-empty">
			Nothing to review: no lyric verdicts in state.db yet. Run
			<code>python -m apps.lyrics ingest-state</code> on a finished bench run to populate the queue.
		</p>
	{:else if item}
		<div class="toolrow">
			<span
				class="m"
				data-testid="review-position"
				title="Position of this item in the triage queue (GET /api/v1/lyrics?order=suspect, at most {LIMIT} rows), most suspect first"
				>{positionText(index, rows.length)}</span
			>
			<span class="m" title="Items skipped with 0 in this session; skips are not saved"
				>{skipped.size} skipped</span
			>
			<span class="m" title="Items labeled; label writes are {NOT_IMPLEMENTED}">0 labeled</span>
			<span class="bar" title="{pct}% of the way through the queue"
				><span style="width:{pct}%"></span></span
			>
		</div>
		<div
			class="review"
			role="group"
			aria-label="Review item. 1 to 4 labels it, 0 skips, W or S moves."
		>
			<div class="rv-h">
				<b title={item.stable_id}>{item.title ?? item.stable_id}</b>
				{#if item.artist}<span class="m">{item.artist}</span>{/if}
				{#if lineState.kind === 'ok' && lineState.line}
					<span
						class="m"
						title="Start and end of this line in the track (aligned word timings), line {lineState
							.line.line_no}"
						>at {fmtTime(lineState.line.start_s)} - {fmtTime(lineState.line.end_s)}</span
					>
				{/if}
				<button
					class="btn cue"
					type="button"
					disabled={cueBusy || lineState.kind !== 'ok' || lineState.line === null}
					title="Play this line in headphones (preview cue path only; never touches a deck or the master)"
					onclick={() => void cueLine()}
				>
					<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"
						><path
							d="M4 14v-2a8 8 0 0 1 16 0v2M4 14h3v6H5a1 1 0 0 1-1-1zM20 14h-3v6h2a1 1 0 0 0 1-1z"
							fill="none"
							stroke="currentColor"
							stroke-width="2"
							stroke-linejoin="round"
						/></svg
					>
					Cue line
				</button>
			</div>
			<div class="meta">
				<span title="Lyric provider and match method that supplied this text"
					>source: {item.source ?? '-'}</span
				>
				<span
					class="chip v-{item.effective}"
					title="Effective verdict (a human override beats the computed one)">{item.effective}</span
				>
				{#if item.pct_witness_red !== null && item.pct_witness_red !== undefined}
					<span class="warn-t" title="Share of words the independent ASR witness distrusts"
						>{Math.round(item.pct_witness_red * 1000) / 10}% red</span
					>
				{/if}
				{#if lineState.kind === 'ok' && lineState.line}
					<span
						class="num"
						data-testid="review-red-words"
						use:tip={{ title: 'Red words', body: ['Words on this line the witness distrusts'] }}
						>{lineState.line.n_red} red word(s)</span
					>
				{/if}
			</div>
			{#if lineState.kind === 'loading'}
				<p class="copy">Loading line...</p>
			{:else if lineState.kind === 'err'}
				<div class="error">LINE LOAD FAILED: {lineState.message}</div>
			{:else if lineState.line === null}
				<p class="copy" data-testid="review-no-line">
					No line-level lyrics stored for this track.{#if lineState.missing}
						Server: {lineState.missing}{/if}
				</p>
			{:else}
				<div class="line" data-testid="review-line">{lineState.line.text}</div>
			{/if}
			<div class="labels" role="group" aria-label="Label">
				{#each REVIEW_LABELS as l (l.id)}
					<button
						type="button"
						class="btn"
						aria-disabled={!LABELS_WRITABLE}
						title={LABELS_WRITABLE ? l.text : NOT_IMPLEMENTED}
						onclick={() => (notice = `Label "${l.text}": ${NOT_IMPLEMENTED}`)}
						><kbd>{l.key}</kbd> {l.text}</button
					>
				{/each}
			</div>
			{#if notice}<p class="notice" role="status">{notice}</p>{/if}
		</div>
		<p class="m">
			Keys: 1-4 label (not implemented yet), 0 skips, W/S or Up/Down step back and forward; with a
			modifier they jump to the first or last item. A focused text field keeps its keys.
		</p>
	{/if}
</section>

<style>
	.review-panel {
		max-width: 900px;
	}
	h3 {
		font-size: 1rem;
		color: var(--accent);
		margin: 0 0 0.5rem 0;
	}
	.m {
		color: var(--muted);
		font-size: 0.75rem;
	}
	.copy {
		color: var(--muted);
		font-size: 0.8rem;
	}
	.toolrow {
		display: flex;
		gap: 12px;
		align-items: center;
		margin-bottom: 8px;
	}
	.bar {
		flex: 1;
		min-width: 60px;
		height: 4px;
		background: var(--chip-bg);
		border-radius: 2px;
		overflow: hidden;
	}
	.bar span {
		display: block;
		height: 100%;
		background: var(--accent);
	}
	.review {
		border: 1px solid var(--border);
		border-left: 3px solid var(--st-warn);
		border-radius: 6px;
		padding: 12px;
		display: flex;
		flex-direction: column;
		gap: 10px;
	}
	.review:focus-visible {
		outline: 2px solid var(--accent);
	}
	.rv-h {
		display: flex;
		gap: 10px;
		align-items: center;
		flex-wrap: wrap;
	}
	.rv-h .cue {
		margin-left: auto;
	}
	.meta {
		display: flex;
		gap: 10px;
		align-items: center;
		font-size: 0.75rem;
		color: var(--muted);
	}
	.warn-t {
		color: var(--st-warn);
	}
	.chip {
		border: 1px solid var(--border);
		border-radius: 999px;
		padding: 0.05rem 0.5rem;
	}
	.chip.v-vocal {
		color: var(--st-ok);
		border-color: var(--st-ok);
	}
	.line {
		font-family: var(--mono, monospace);
		background: var(--chip-bg);
		padding: 8px 10px;
		border-radius: 4px;
		color: var(--fg);
	}
	.labels {
		display: flex;
		gap: 6px;
		flex-wrap: wrap;
	}
	.btn {
		display: inline-flex;
		gap: 5px;
		align-items: center;
		background: var(--chip-bg);
		color: var(--fg);
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: 0.2rem 0.55rem;
		font-size: 0.78rem;
		cursor: pointer;
	}
	.btn[aria-disabled='true'] {
		opacity: 0.5;
		cursor: not-allowed;
	}
	.btn:disabled {
		opacity: 0.4;
		cursor: default;
	}
	kbd {
		font-size: 0.7rem;
		border: 1px solid var(--border);
		border-radius: 3px;
		padding: 0 3px;
	}
	.notice {
		color: var(--st-warn);
		font-size: 0.78rem;
		margin: 0;
	}
	.error {
		background: var(--danger);
		color: #fff;
		padding: 0.6rem 0.8rem;
		border-radius: 6px;
		font-size: 0.8rem;
		font-weight: 600;
		white-space: pre-wrap;
	}
	code {
		background: var(--chip-bg);
		padding: 0.05rem 0.35rem;
		border-radius: 4px;
	}
</style>
