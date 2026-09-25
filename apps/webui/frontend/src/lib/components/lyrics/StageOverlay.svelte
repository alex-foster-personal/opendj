<!--
  STAGE view - full-screen karaoke lyric overlay, mounted ONCE in the root
  layout (SettingsOverlay precedent) and driven entirely by stageState from
  $lib/lyrics/stage-store.svelte.

  Two clock sources, one frame path (stage-frame.ts):
    - track mode (stageState.deck === null): the overlay owns an <audio> on
      /api/v1/tracks/{id}/audio. The audio URL is HEAD-probed BEFORE a player
      renders - with preload="none" a 404 never fires an error event, so an
      unresolvable file would otherwise leave a dead transport on screen.
    - performance mode (stageState.deck set): follows that deck's presented
      engine clock (DeckState.position_ms). No audio element - the deck is
      already audible.

  rAF discipline (the starvation trap): structural DOM only changes when the
  resolved anchor changes; the per-frame work writes ONE CSS custom property
  (--word-fill) and samples the numeric readouts at 10Hz.
-->
<script lang="ts">
	import { API_BASE } from '$lib/api';
	import { trackApiPath } from '$lib/rb/track-source';
	import { getDeckState } from '$lib/rb/audio-engine.svelte';
	import { lyricEntry, loadLyrics } from '$lib/lyrics/lyrics-cache.svelte';
	import { closeStage, stageState, type StageDeck } from '$lib/lyrics/stage-store.svelte';
	import {
		anchorKey,
		buildFrame,
		buildStageScore,
		formatClock,
		lineFitScale,
		resolveAnchor,
		showInterludeCard,
		wordProgress01,
		type StageFrame,
		type StageFrameLine,
		type StageFrameWord,
		type StageScore
	} from '$lib/lyrics/stage-frame';

	let stageEl = $state<HTMLDivElement | null>(null);
	let audioEl = $state<HTMLAudioElement | null>(null);
	/** Null until probed, so no player flashes before we know it can play. */
	let audioOk = $state<boolean | null>(null);
	/** Rebuilt only when the anchor changes - never respread per tick. */
	let frame = $state<StageFrame | null>(null);
	/** 10Hz-sampled numeric readouts (R2: readouts sample, lyrics never do). */
	let readoutMs = $state(0);
	let readoutGapMs = $state<number | null>(null);
	let _lastKey = '';
	let _lastReadoutAt = 0;

	const entry = $derived(stageState.stableId === null ? null : lyricEntry(stageState.stableId));
	const audioUrl = $derived(
		stageState.stableId === null
			? null
			: `${API_BASE}${trackApiPath(stageState.stableId, '/audio')}` // spec 4b: stick ids stream from the usb route
	);
	const perfDeck = $derived(stageState.deck === null ? null : getDeckState(stageState.deck));
	const deckMismatch = $derived(
		perfDeck !== null && perfDeck.stable_id !== stageState.stableId
	);
	const verdict = $derived(entry?.track?.verdict ?? null);

	const scoreResult = $derived.by((): { score: StageScore | null; error: string | null } => {
		if (entry === null || entry.state !== 'loaded' || entry.track === null) {
			return { score: null, error: null };
		}
		try {
			return { score: buildStageScore(entry.track), error: null };
		} catch (exc) {
			return { score: null, error: exc instanceof Error ? exc.message : String(exc) };
		}
	});

	// Load lyrics through the shared cache whenever the stage opens on a track.
	$effect(() => {
		if (!stageState.open || stageState.stableId === null) return;
		void loadLyrics(stageState.stableId);
	});

	// Track mode: HEAD-probe the audio before rendering a player.
	$effect(() => {
		if (!stageState.open || stageState.deck !== null || audioUrl === null) return;
		audioOk = null;
		let cancelled = false;
		void fetch(audioUrl, { method: 'HEAD' })
			.then((r) => {
				if (!cancelled) audioOk = r.ok;
			})
			.catch(() => {
				if (!cancelled) audioOk = false;
			});
		return () => {
			cancelled = true;
		};
	});

	// New score (track changed / reloaded): drop the stale frame.
	$effect(() => {
		void scoreResult;
		_lastKey = '';
		frame = null;
	});

	// The clock loop. Reads the presented position, resolves the anchor, and
	// only rebuilds the frame when the anchor actually changed.
	$effect(() => {
		if (!stageState.open) return;
		const score = scoreResult.score;
		if (score === null) return;
		let rafId = 0;
		const tick = (): void => {
			const ms = _presentedMs();
			if (ms !== null) _applyClock(score, ms);
			rafId = requestAnimationFrame(tick);
		};
		rafId = requestAnimationFrame(tick);
		return () => cancelAnimationFrame(rafId);
	});

	// Track mode with unresolvable audio: no clock will ever tick, so build
	// one honest frame at 0 and say why it will not move.
	$effect(() => {
		if (!stageState.open || stageState.deck !== null || audioOk !== false) return;
		const score = scoreResult.score;
		if (score === null) return;
		_applyClock(score, 0);
	});

	//------------------------------------------------------------- _helpers

	function _presentedMs(): number | null {
		const deckId: StageDeck | null = stageState.deck;
		if (deckId !== null) {
			const deck = getDeckState(deckId);
			// Track swapped out from under the stage: freeze rather than follow
			// another track's clock.
			if (deck.stable_id !== stageState.stableId) return null;
			return deck.position_ms;
		}
		if (audioEl === null) return null;
		return audioEl.currentTime * 1000;
	}

	function _applyClock(score: StageScore, ms: number): void {
		const anchor = resolveAnchor(score, ms);
		const key = anchorKey(anchor);
		if (key !== _lastKey) {
			_lastKey = key;
			frame = buildFrame(score, ms, anchor);
		}
		// Per-frame work is ONE CSS custom property - the word wipe.
		stageEl?.style.setProperty(
			'--word-fill',
			wordProgress01(score, anchor.timedWordIndex, ms).toFixed(4)
		);
		const now = performance.now();
		if (now - _lastReadoutAt >= 100) {
			_lastReadoutAt = now;
			readoutMs = ms;
			readoutGapMs = anchor.gapMs === null ? null : Math.round(anchor.gapMs);
		}
	}

	function onWindowKeydown(e: KeyboardEvent): void {
		if (!stageState.open || e.key !== 'Escape') return;
		// The settings overlay's capture-phase handler claims Esc first when it
		// is open above the stage.
		if (e.defaultPrevented) return;
		const target = e.target as HTMLElement | null;
		if (
			target !== null &&
			(target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)
		) {
			return;
		}
		e.preventDefault();
		closeStage();
	}

	function lineClickable(line: StageFrameLine): boolean {
		return stageState.deck === null && audioOk === true && line.start_ms !== null;
	}

	function seekToLine(line: StageFrameLine): void {
		if (!lineClickable(line) || audioEl === null || line.start_ms === null) return;
		audioEl.currentTime = line.start_ms / 1000;
		void audioEl.play();
	}

	function lineIsLowConfidence(line: StageFrameLine): boolean {
		return line.band === 'bad' || line.band === 'uncertain';
	}

	function lineTitle(line: StageFrameLine): string {
		const parts: string[] = [];
		if (lineIsLowConfidence(line)) {
			parts.push(
				`${line.band} line - witness evidence: ${line.n_red} of ${line.n_judged} ` +
					'ASR-judged words contradicted or lost, so words/timing here may be wrong'
			);
		}
		if (lineClickable(line) && line.start_ms !== null) {
			parts.push(`click to seek to ${formatClock(line.start_ms / 1000)}`);
		} else if (stageState.deck !== null) {
			parts.push('seek disabled - performance mode follows the deck transport');
		} else if (audioOk !== true) {
			parts.push('seek disabled - audio for this track is not playable here');
		} else {
			parts.push('seek disabled - this line carries no aligned timing');
		}
		return parts.join(' - ') || line.text;
	}

	const WITNESS_TITLES: Record<string, string> = {
		agree: 'the independent ASR witness heard this word at this time',
		drift: 'the witness heard it, but more than 1s away from this position',
		contradict: 'the witness heard something else here',
		lost: 'inside a run of 5+ words the witness never heard',
		unheard: 'the witness did not hear this word (a single word, not a run)',
		unmatchable: 'nothing comparable in the transcript (punctuation, vocables)'
	};

	function wordTitle(w: StageFrameWord): string {
		if (w.state === 'untimed') return 'no aligned timing for this word - it cannot be swept';
		const at = w.start_ms === null ? '' : `${formatClock(w.start_ms / 1000)} - `;
		const witness =
			w.witness === null ? 'no witness verdict recorded' : (WITNESS_TITLES[w.witness] ?? w.witness);
		return `${at}${witness}`;
	}

	function wordIsSuspect(w: StageFrameWord): boolean {
		return w.witness === 'contradict' || w.witness === 'lost';
	}
</script>

<svelte:window onkeydown={onWindowKeydown} />

{#if stageState.open}
	<div
		class="stage"
		bind:this={stageEl}
		role="dialog"
		aria-modal="true"
		aria-label="Stage lyrics view"
	>
		<header class="stage-head">
			<div class="stage-meta">
				{#if stageState.deck !== null}
					<span
						class="chip"
						title="Performance mode - the stage follows this deck's presented engine clock"
					>
						DECK {stageState.deck}
					</span>
				{/if}
				{#if verdict !== null}
					{#if verdict.language_iso3 !== null}
						<span class="chip" title="Detected language of the lyric text (ISO 639-3)">
							{verdict.language_iso3}
						</span>
					{/if}
					{#if verdict.source !== null}
						<span class="chip" title="Where the lyric text came from and how it was matched">
							{verdict.source}
						</span>
					{/if}
				{/if}
			</div>
			<span
				class="stage-clock"
				title="Presented source position of the stage clock (audio element in track mode, deck engine in performance mode), sampled at 10Hz"
			>
				{formatClock(readoutMs / 1000)}
			</span>
			<button
				type="button"
				class="stage-close"
				onclick={() => closeStage()}
				aria-label="Close stage view"
				title="Close the stage view (Esc)"
			>
				×
			</button>
		</header>

		<div class="stage-body">
			{#if stageState.stableId === null}
				<p class="stage-msg">No track selected for the stage.</p>
			{:else if entry === null || entry.state === 'loading'}
				<p class="stage-msg">Loading lyrics…</p>
			{:else if entry.state === 'error'}
				<p class="stage-msg err">
					Lyrics fetch failed: {entry.error}
					<button
						type="button"
						class="stage-retry"
						onclick={() => stageState.stableId !== null && void loadLyrics(stageState.stableId)}
					>
						retry
					</button>
				</p>
			{:else if entry.state === 'none'}
				<p class="stage-msg">
					No lyric data for this track yet - the alignment pipeline has not produced words.
				</p>
			{:else if scoreResult.error !== null}
				<p class="stage-msg err">Lyrics unusable for the stage: {scoreResult.error}</p>
			{:else if deckMismatch}
				<p class="stage-msg err">
					Deck {stageState.deck} no longer has this track loaded - the stage clock has stopped.
				</p>
			{:else if frame !== null}
				{@const cur = frame.lines.current}
				{@const prev = frame.lines.prev}
				{@const next = frame.lines.next}
				{#if showInterludeCard(frame.state)}
					<div class="interlude">
						{#if frame.state === 'outro'}
							<p class="interlude-label">end of lyrics</p>
						{:else}
							<p class="interlude-label">
								{frame.state === 'lead-in' ? 'lyrics begin' : 'instrumental'}
							</p>
							{#if readoutGapMs !== null}
								<p
									class="countdown"
									title="Seconds until the next aligned word begins, measured from the presented clock to that word's onset (only ever shown while NO word is active)"
								>
									{Math.max(0, Math.ceil(readoutGapMs / 1000))}s
								</p>
							{/if}
						{/if}
					</div>
				{/if}

				<div class="lines">
					{#if prev !== null}
						<button
							type="button"
							class="line neighbor"
							class:low={lineIsLowConfidence(prev)}
							disabled={!lineClickable(prev)}
							title={lineTitle(prev)}
							onclick={() => seekToLine(prev)}
						>
							{prev.text}
						</button>
					{/if}
					{#if cur !== null}
						<div
							class="line current"
							class:low={lineIsLowConfidence(cur)}
							title={lineIsLowConfidence(cur) ? lineTitle(cur) : undefined}
							style={`font-size: calc(clamp(1.8rem, 5.2vw, 4.4rem) * ${lineFitScale(cur.text)})`}
						>
							{#each cur.words as w (w.idx)}
								<span
									class="word st-{w.state}"
									class:suspect={wordIsSuspect(w)}
									title={wordTitle(w)}>{w.text}</span
								>
							{/each}
						</div>
					{/if}
					{#if next !== null}
						<button
							type="button"
							class="line neighbor"
							class:low={lineIsLowConfidence(next)}
							disabled={!lineClickable(next)}
							title={lineTitle(next)}
							onclick={() => seekToLine(next)}
						>
							{next.text}
						</button>
					{/if}
				</div>
			{:else}
				<p class="stage-msg">Waiting for the clock…</p>
			{/if}
		</div>

		{#if stageState.deck === null && stageState.stableId !== null}
			<footer class="stage-foot">
				{#if audioOk === true && audioUrl !== null}
					<audio
						bind:this={audioEl}
						src={audioUrl}
						controls
						preload="metadata"
						onerror={() => (audioOk = false)}
					></audio>
				{:else if audioOk === false}
					<p class="stage-msg small">
						Audio for this track could not be resolved on disk, so the stage cannot play or
						follow it (library relink problem - see docs/library-availability.md). The lines
						above are shown at position 0:00 and will not move.
					</p>
				{:else}
					<p class="stage-msg small">Probing audio…</p>
				{/if}
			</footer>
		{/if}
	</div>
{/if}

<style>
	.stage {
		position: fixed;
		inset: 0;
		z-index: 350; /* below the settings overlay (400) so Cmd+, still wins */
		display: flex;
		flex-direction: column;
		background:
			radial-gradient(ellipse at 50% 35%, rgba(255, 180, 58, 0.09), transparent 60%),
			#05070c;
		color: #f6f8ff;
		animation: stage-fade 140ms ease-out;
	}
	.stage-head {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 12px 16px;
	}
	.stage-meta {
		display: flex;
		gap: 6px;
		flex: 1;
		min-width: 0;
	}
	.chip {
		font-size: 0.72rem;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: rgba(246, 248, 255, 0.65);
		border: 1px solid rgba(246, 248, 255, 0.22);
		border-radius: 999px;
		padding: 2px 10px;
		white-space: nowrap;
	}
	.stage-clock {
		font-variant-numeric: tabular-nums;
		color: rgba(246, 248, 255, 0.55);
		font-size: 0.9rem;
	}
	.stage-close {
		width: 36px;
		height: 36px;
		border-radius: 10px;
		border: 1px solid rgba(246, 248, 255, 0.25);
		background: rgba(246, 248, 255, 0.06);
		color: #f6f8ff;
		font-size: 1.3rem;
		line-height: 1;
		cursor: pointer;
	}
	.stage-close:hover {
		background: rgba(246, 248, 255, 0.14);
	}
	.stage-body {
		flex: 1;
		min-height: 0;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 2.2rem;
		padding: 0 6vw;
		text-align: center;
		overflow: hidden;
	}
	.stage-msg {
		color: rgba(246, 248, 255, 0.7);
		font-size: 1.05rem;
		max-width: 46rem;
	}
	.stage-msg.err {
		color: #ff8080;
	}
	.stage-msg.small {
		font-size: 0.85rem;
		margin: 0;
	}
	.stage-retry {
		margin-left: 8px;
		padding: 2px 10px;
		border-radius: 8px;
		border: 1px solid rgba(246, 248, 255, 0.3);
		background: transparent;
		color: #f6f8ff;
		cursor: pointer;
	}
	.interlude {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 0.4rem;
	}
	.interlude-label {
		margin: 0;
		font-size: 1rem;
		letter-spacing: 0.28em;
		text-transform: uppercase;
		color: rgba(246, 248, 255, 0.45);
	}
	.countdown {
		margin: 0;
		font-size: 2.6rem;
		font-variant-numeric: tabular-nums;
		color: var(--accent, #ffb43a);
	}
	.lines {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 1.6rem;
		width: 100%;
	}
	.line {
		max-width: 100%;
		border: none;
		background: transparent;
		font: inherit;
		color: inherit;
		text-align: center;
	}
	.line.neighbor {
		font-size: clamp(1rem, 2.2vw, 1.7rem);
		color: rgba(246, 248, 255, 0.38);
		cursor: pointer;
	}
	.line.neighbor:disabled {
		cursor: default;
	}
	.line.neighbor:not(:disabled):hover {
		color: rgba(246, 248, 255, 0.7);
	}
	.line.current {
		display: flex;
		flex-wrap: wrap;
		justify-content: center;
		column-gap: 0.35em;
		row-gap: 0.1em;
		font-weight: 700;
		line-height: 1.15;
	}
	/* Reduced-confidence lines: dimmer, never confidently wrong. */
	.line.low {
		opacity: 0.55;
	}
	.word {
		color: rgba(246, 248, 255, 0.4);
	}
	.word.st-sung {
		color: var(--accent, #ffb43a);
	}
	/* The wipe: a gradient whose split point is the ONE per-frame CSS var. */
	.word.st-active {
		background: linear-gradient(
			90deg,
			var(--accent, #ffb43a) calc(var(--word-fill, 0) * 100%),
			rgba(246, 248, 255, 0.4) calc(var(--word-fill, 0) * 100%)
		);
		-webkit-background-clip: text;
		background-clip: text;
		color: transparent;
	}
	.word.st-untimed {
		color: rgba(246, 248, 255, 0.22);
		font-style: italic;
	}
	.word.suspect {
		text-decoration: underline wavy rgba(224, 82, 82, 0.7);
		text-underline-offset: 0.14em;
	}
	.stage-foot {
		padding: 10px 16px 14px;
		display: flex;
		justify-content: center;
	}
	.stage-foot audio {
		width: min(680px, 92vw);
	}
	@keyframes stage-fade {
		from {
			opacity: 0;
		}
		to {
			opacity: 1;
		}
	}
</style>
