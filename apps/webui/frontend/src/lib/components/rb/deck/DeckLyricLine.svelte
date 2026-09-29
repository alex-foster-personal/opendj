<script lang="ts">
	/**
	 * Deck one-line lyric readout - the compact strip that lives in the slack
	 * BELOW the hot-cue bank. Derived from the af--karaoke-ui-deck spike's
	 * LyricsPanel, trimmed to what fits a deck: the current line (word wipe
	 * when the server band allows it) plus, when `rows` is 2, the inbound
	 * line underneath.
	 *
	 * THE CLOCK CONTRACT
	 *  - word timings are immutable SOURCE-TRACK seconds;
	 *  - `positionSource()` must return the source position of the sample the
	 *    output has ACTUALLY PRESENTED (the engine presented clock,
	 *    DeckState.position_ms). Never wall time. Seeks and rate changes need
	 *    no special case: the next frame simply reads a different position;
	 *  - half-open [start_s, end_s) picks the active word, and the inter-word
	 *    gap is a REAL state with NO active word.
	 *
	 * TICK DISCIPLINE (from the spike's Deck mount): reactive piggyback. The
	 * engine writes DeckState.position_ms on ITS OWN rAF; reading it through
	 * `positionSource()` inside the $effect IS the subscription, so this
	 * component runs exactly once per engine frame and never starts a second
	 * loop. Per frame it writes exactly TWO custom properties on ONE node
	 * (--kar-word-fill, --kar-line-prog); everything else is $state, so a
	 * steady word costs zero DOM work.
	 *
	 * PER-LINE TRUST comes from the server's calibrated witness band, mapped
	 * in build-track.ts: good = word wipe, uncertain = whole-line dimmed,
	 * bad = whole-line amber (never animate confidently over words the
	 * witness distrusts), unjudged = whole-line neutral.
	 */
	import { buildWordLineMap, resolveCursor } from '$lib/rb/lyrics/cursor';
	import type { LyricsCursor } from '$lib/rb/lyrics/cursor';
	import type { LyricsPlaybackState, LyricsTrack } from '$lib/rb/lyrics/types';

	let {
		track,
		entryState,
		error = null,
		positionSource,
		rows
	}: {
		track: LyricsTrack | null;
		/** lyrics-cache state for this deck's track (honest, never mocked). */
		entryState: 'loading' | 'loaded' | 'none' | 'error';
		/** Fetch or adapter failure, rendered as a real failure. */
		error?: string | null;
		positionSource: () => number | null;
		/** 1 = current line only; 2-3 = current plus upcoming preview lines. */
		rows: 1 | 2 | 3;
	} = $props();

	// Per-track derived structure, rebuilt only when the track changes.
	const wordLine: Int32Array | null = $derived(track === null ? null : buildWordLineMap(track));

	// ------------------------------------------------------------ live state
	// Assigning an unchanged primitive is a no-op in Svelte 5, so a steady
	// word costs zero DOM work.
	let lineIndex: number | null = $state(null);
	let wordIndex: number | null = $state(null);
	let nextLineIndex: number | null = $state(null);
	let playState: LyricsPlaybackState = $state('preroll');
	/** Highest word index already finished. Survives inter-word gaps, so a
	 *  50ms gap mid-line does not blank the words already sung. */
	let sungThrough: number = $state(-1);
	/** Countdown quantised to 0.1s so the text node changes 10x/s, not 60x/s. */
	let countdownTenths: number | null = $state(null);

	let rootEl: HTMLDivElement | null = $state(null);
	let hint = 0;

	function tickAt(t: number | null): void {
		if (track === null || wordLine === null) return;
		if (t === null || !Number.isFinite(t)) return;

		const cursor: LyricsCursor = resolveCursor(track, wordLine, t, hint);
		hint = cursor.hint;

		// Two custom properties on one node. This is the whole per-frame cost.
		if (rootEl !== null) {
			rootEl.style.setProperty('--kar-word-fill', String(cursor.wordProgress ?? 0));
			rootEl.style.setProperty('--kar-line-prog', String(cursor.lineProgress));
		}

		playState = cursor.state;
		lineIndex = cursor.lineIndex;
		wordIndex = cursor.wordIndex;
		nextLineIndex = cursor.nextLineIndex;
		countdownTenths =
			cursor.nextVocalInMs === null ? null : Math.max(0, Math.round(cursor.nextVocalInMs / 100));
		sungThrough =
			cursor.wordIndex !== null
				? cursor.wordIndex - 1
				: cursor.state === 'preroll'
					? -1
					: cursor.hint;
	}

	/** Reactive drive: reading the engine's presented clock subscribes this
	 * effect to the engine's own rAF. `tickAt` writes only state this effect
	 * never reads back, so it cannot re-trigger itself. */
	$effect(() => {
		tickAt(positionSource());
	});

	const currentLine = $derived(
		track !== null && lineIndex !== null ? track.lines[lineIndex] : null
	);
	const nextLine = $derived(
		track !== null && nextLineIndex !== null ? track.lines[nextLineIndex] : null
	);
	const thirdLineIndex = $derived(
		nextLineIndex === null ? null : nextLineIndex + 1 < (track?.lines.length ?? 0) ? nextLineIndex + 1 : null
	);
	const thirdLine = $derived(
		track !== null && thirdLineIndex !== null ? track.lines[thirdLineIndex] : null
	);
	/** Words of the current line, rebuilt only on a line change. */
	const lineWords = $derived(
		track === null || currentLine === null
			? []
			: track.words
					.slice(currentLine.first_word, currentLine.last_word + 1)
					.map((w, i) => ({ index: currentLine.first_word + i, text: w.word }))
	);

	// ------------------------------------------------------------- titles
	const countdownTip =
		'seconds of source-track time until the next sung word starts, from the presented clock';
	const bandTip = $derived.by(() => {
		if (currentLine === null) return '';
		const judged =
			currentLine.n_judged === 0
				? 'the ASR witness judged none of its words'
				: `${currentLine.n_red} of ${currentLine.n_judged} witness-judged words are in ` +
					`the red classes` +
					(currentLine.quality === null
						? ''
						: ` (${Math.round(currentLine.quality * 100)}% clean)`);
		if (currentLine.band === 'good') {
			return `witness band GOOD: per-word wipe follows the aligner onsets. ${judged}.`;
		}
		if (currentLine.band === 'uncertain') {
			return (
				`witness band UNCERTAIN: the whole line lights on entry, dimmed - word onsets ` +
				`are not trusted enough to point at a syllable. ${judged}.`
			);
		}
		if (currentLine.band === 'bad') {
			return (
				`witness band BAD: the ASR witness contradicts this line's alignment, so it ` +
				`lights whole and never animates per word. ${judged}.`
			);
		}
		return (
			`witness band UNJUDGED: the whole line lights on entry; the witness has not ` +
			`judged these words, so per-word timing is neither claimed nor denied.`
		);
	});
	const trackTip = $derived(
		track === null
			? ''
			: `deck lyric line - bands across ${track.lines.length} lines: ` +
				`${track.band_counts.good} good / ${track.band_counts.uncertain} uncertain / ` +
				`${track.band_counts.bad} bad / ${track.band_counts.unjudged} unjudged; ` +
				`${track.word_fidelity_lines} animate per word (calibrated witness bands, ` +
				`served by the API)`
	);

	function countdownText(tenths: number): string {
		return `${(tenths / 10).toFixed(1)}s`;
	}
</script>

<div
	class="dkl"
	class:dkl-compact={rows === 1}
	data-deck-lyric-state={entryState}
	data-state={playState}
	data-band={currentLine?.band ?? ''}
	bind:this={rootEl}
	title={trackTip}
>
	{#if error !== null}
		<div class="dkl-row dkl-msg" title={`lyrics unavailable: ${error}`}>
			<span class="dkl-tag dkl-tag-err">LYRICS ERR</span>
			<span class="dkl-dim">{error}</span>
		</div>
	{:else if entryState === 'loading'}
		<div class="dkl-row dkl-msg" title="lyric word payload is loading from the daemon">
			<span class="dkl-dim">lyrics&hellip;</span>
		</div>
	{:else if entryState === 'none'}
		<div
			class="dkl-row dkl-msg"
			title="the lyrics pipeline has no data for this track yet - see the /lyrics API"
		>
			<span class="dkl-tag">NO LYRIC DATA</span>
		</div>
	{:else if track === null}
		<!-- entryState 'loaded' but no adapted track and no error: unreachable
		     by construction in Deck.svelte; render honestly anyway. -->
		<div class="dkl-row dkl-msg" title="lyric entry loaded but no track was adapted">
			<span class="dkl-tag dkl-tag-err">LYRICS ERR</span>
			<span class="dkl-dim">empty adapted track</span>
		</div>
	{:else if track.verdict === 'no-lyrics'}
		<div
			class="dkl-row dkl-msg"
			title="calibrated verdict: no lyrics (stem vocal coverage below the pinned threshold in apps/lyrics/vocal_presence.py). Words exist in the payload but are not trusted for display."
		>
			<span class="dkl-tag">NO LYRICS</span>
			<span class="dkl-dim">instrumental verdict</span>
		</div>
	{:else}
		{#if playState === 'preroll' || playState === 'gap'}
			<div class="dkl-row dkl-current dkl-gap">
				<span class="dkl-cd-label">VOCAL IN</span>
				<span class="dkl-cd-value" title={countdownTip}>
					{countdownTenths === null ? '--' : countdownText(countdownTenths)}
				</span>
			</div>
		{:else if playState === 'outro'}
			<div class="dkl-row dkl-current dkl-gap">
				<span class="dkl-cd-label dkl-cd-done" title="the last sung word of this track has ended"
					>VOCAL DONE</span
				>
			</div>
		{:else if currentLine !== null && currentLine.fidelity === 'word'}
			{#key lineIndex}
				<div class="dkl-row dkl-current" title={bandTip}>
					{#each lineWords as word (word.index)}<span
							class="dkl-word"
							class:dkl-sung={word.index <= sungThrough}
							class:dkl-live={word.index === wordIndex}
							data-word={word.text}>{word.text}</span
						>{' '}{/each}
				</div>
			{/key}
		{:else if currentLine !== null}
			{#key lineIndex}
				<div
					class="dkl-row dkl-current dkl-whole"
					class:dkl-uncertain={currentLine.band === 'uncertain'}
					class:dkl-bad={currentLine.band === 'bad'}
					title={bandTip}
				>
					{#if currentLine.band !== 'unjudged'}<span
							class="dkl-approx"
							title="approximate timing: this line does not carry trusted per-word onsets"
							>~</span
						>{/if}{currentLine.text}
				</div>
			{/key}
		{/if}

		{#if rows >= 2}
			<div class="dkl-row dkl-next" title="the line coming up next">
				{#if nextLine !== null}<span class="dkl-next-mark">&gt;</span>{nextLine.text}{/if}
			</div>
		{/if}
		{#if rows === 3}
			<div class="dkl-row dkl-next dkl-next2" title="the line after next">
				{#if thirdLine !== null}<span class="dkl-next-mark">&gt;&gt;</span>{thirdLine.text}{/if}
			</div>
		{/if}
	{/if}
</div>

<style>
	.dkl {
		--kar-word-fill: 0;
		--kar-line-prog: 0;
		--dkl-dim: var(--rb-text-dim, #7a8088);
		--dkl-sung: color-mix(in srgb, var(--rb-accent, #2f6fd6) 45%, var(--rb-text, #c8cdd2));
		--dkl-live: #ffffff;

		display: flex;
		flex-direction: column;
		gap: 0;
		min-width: 0;
		min-height: 0;
		overflow: hidden;
		font-family: var(--rb-font);
	}
	.dkl-row {
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		min-width: 0;
		flex: 0 0 auto;
	}
	.dkl-current {
		height: 18px;
		line-height: 18px;
		font-size: 12.5px;
		font-weight: 600;
		letter-spacing: -0.01em;
		color: var(--dkl-dim);
		animation: dkl-line-in 130ms ease-out;
	}
	/* Compact one-row mode: the canonical 4-deck layout leaves only 15px of
	 * slack below the cue bank, so the single row must fit 14px. */
	.dkl-compact .dkl-current {
		height: 14px;
		line-height: 14px;
		font-size: 11.5px;
	}
	.dkl-compact .dkl-msg {
		height: 14px;
		line-height: 14px;
	}
	.dkl-compact .dkl-cd-value {
		font-size: 11px;
	}
	.dkl-next {
		height: 12px;
		line-height: 12px;
		font-size: 10px;
		color: color-mix(in srgb, var(--dkl-dim) 85%, transparent);
	}
	.dkl-next-mark {
		color: var(--rb-accent, #2f6fd6);
		font-weight: 700;
		margin-right: 3px;
	}

	/* ------------------------------------------------------- word wipe */
	.dkl-word {
		position: relative;
		color: var(--dkl-dim);
		white-space: pre;
	}
	.dkl-sung {
		color: var(--dkl-sung);
	}
	.dkl-live {
		color: color-mix(in srgb, var(--dkl-sung) 45%, var(--dkl-dim));
	}
	/* The karaoke wipe: an overlaid copy of the same glyphs, clipped to the
	   word's own [start,end) progress. Driven by --kar-word-fill, which is
	   (with --kar-line-prog) the only thing written per frame. */
	.dkl-live::after {
		content: attr(data-word);
		position: absolute;
		left: 0;
		top: 0;
		width: calc(var(--kar-word-fill) * 100%);
		overflow: hidden;
		color: var(--dkl-live);
		text-shadow: 0 0 8px var(--rb-accent-glow, rgba(47, 111, 214, 0.55));
		white-space: pre;
		pointer-events: none;
	}

	/* ------------------------------------------ whole-line (non-good bands) */
	.dkl-whole {
		color: var(--dkl-live);
		animation: dkl-line-flare 220ms ease-out;
	}
	.dkl-whole.dkl-uncertain {
		color: color-mix(in srgb, var(--dkl-live) 55%, var(--dkl-dim));
	}
	.dkl-whole.dkl-bad {
		color: var(--rb-orange, #e8a13a);
	}
	.dkl-approx {
		color: var(--rb-orange, #e8a13a);
		margin-right: 4px;
		opacity: 0.85;
	}

	/* --------------------------------------------------- gap / countdown */
	.dkl-gap {
		display: flex;
		align-items: baseline;
		gap: 5px;
	}
	.dkl-cd-label {
		font-size: 9px;
		font-weight: 700;
		letter-spacing: 0.08em;
		color: var(--dkl-dim);
	}
	.dkl-cd-done {
		color: color-mix(in srgb, var(--dkl-dim) 70%, transparent);
	}
	.dkl-cd-value {
		font-size: 12px;
		font-weight: 700;
		font-variant-numeric: tabular-nums;
		color: var(--rb-accent, #2f6fd6);
		cursor: help;
	}

	/* -------------------------------------------------- honest messages */
	.dkl-msg {
		display: flex;
		align-items: center;
		gap: 5px;
		height: 18px;
		line-height: 18px;
		cursor: help;
	}
	.dkl-tag {
		font-size: 9px;
		font-weight: 700;
		letter-spacing: 0.06em;
		color: var(--dkl-dim);
	}
	.dkl-tag-err {
		color: var(--rb-orange, #e8a13a);
	}
	.dkl-dim {
		font-size: 10px;
		color: var(--dkl-dim);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	@keyframes dkl-line-in {
		from {
			opacity: 0.25;
			transform: translateY(3px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}
	@keyframes dkl-line-flare {
		from {
			opacity: 0.3;
			transform: translateY(3px);
		}
		60% {
			opacity: 1;
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}
</style>
