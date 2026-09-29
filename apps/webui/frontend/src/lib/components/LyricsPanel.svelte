<!--
  Karaoke lyrics inspector (operational-plan option A).

  Answers one question at a glance: can I trust this track's lyrics? The QC
  banner carries the calibrated verdict and its evidence; the words are tinted
  by the INDEPENDENT ASR witness, never by the aligner's own confidence, which
  round 3a proved meaningless once an alignment path detaches.

  Playback drives the highlight from the daemon's Range-capable audio
  endpoint (GET /api/v1/tracks/{stable_id}/audio, apps/webui/server/routes/
  rb_assets.py). Where the audio file cannot be resolved, the player is
  hidden and the reason is STATED,
  rather than leaving a dead transport on screen.
-->
<script lang="ts">
	import { untrack } from 'svelte';

	import { API_BASE } from '$lib/api';
	import { trackApiPath } from '$lib/rb/track-source';
	import {
		putLyricOverride,
		type KaraokeTrack,
		type KaraokeWord,
		type LyricVerdictValue
	} from '$lib/api-karaoke';
	import { lyricsCache } from '$lib/lyrics/lyrics-cache.svelte';
	import { pushToast } from '$lib/stores.svelte';

	interface Props {
		stableId: string;
	}
	let { stableId }: Props = $props();

	// Reads through the SHARED lyric cache (one fetch per track per session,
	// shared with the deck line and waveform lanes - the track page previously
	// fetched twice). Overrides write back through the cache so every consumer
	// updates at once.
	const entry = $derived(lyricsCache.entry(stableId));
	const data = $derived<KaraokeTrack | null>(
		entry !== null && entry.state === 'loaded' ? entry.track : null
	);
	const loaded = $derived(entry !== null && entry.state !== 'loading');
	const failure = $derived(entry !== null && entry.state === 'error' ? entry.error : null);

	/** Null until probed, so no player flashes before we know it can play. */
	let audioOk = $state<boolean | null>(null);
	/** Why the probe said no, verbatim. A dead player with no reason is the
	 *  failure mode this whole probe exists to avoid. */
	let audioReason = $state<string | null>(null);
	let currentTime = $state(0);
	let audioEl = $state<HTMLAudioElement | null>(null);

	// Spec 4b: a stick id streams from /api/v1/usb/tracks/{id}/audio.
	const audioUrl = $derived(`${API_BASE}${trackApiPath(stableId, '/audio')}`);

	/** Index of the word currently sounding, or -1. Words carry absolute times,
	 *  so a linear scan is honest and cheap at these lengths (<1k words). */
	const activeIdx = $derived.by(() => {
		if (data === null) return -1;
		for (let i = data.words.length - 1; i >= 0; i--) {
			const w = data.words[i];
			if (w.start_s !== null && w.start_s !== undefined && w.start_s <= currentTime) return i;
		}
		return -1;
	});

	const suspectCount = $derived(
		data === null
			? 0
			: data.words.filter((w) => w.witness === 'contradict' || w.witness === 'lost').length
	);

	/** Kick the shared load and probe the audio. With preload="none" a 404
	 *  never fires an error event, so an unresolvable file would otherwise
	 *  leave a dead 0:00/0:00 transport on screen with no explanation. */
	async function _load(id: string, url: string): Promise<void> {
		void lyricsCache.load(id);
		audioOk = null;
		audioReason = null;
		try {
			const r = await fetch(url, { method: 'HEAD' });
			audioOk = r.ok;
			audioReason = r.ok ? null : `the daemon answered HTTP ${r.status} for this track's audio`;
		} catch (exc) {
			// NOT swallowed: the reason is rendered below. A HEAD that cannot
			// even reach the daemon is a different fact from a 404, and the
			// operator needs to be able to tell them apart.
			audioOk = false;
			audioReason = exc instanceof Error ? exc.message : String(exc);
		}
	}

	function witnessClass(w: KaraokeWord): string {
		if (w.witness === 'contradict' || w.witness === 'lost') return 'w-bad';
		else if (w.witness === 'agree') return 'w-good';
		else return 'w-mid';
	}

	const WITNESS_MEANINGS: Record<string, string> = {
		agree: 'the independent ASR witness heard this word at this time',
		drift: 'the witness heard it, but more than 1s away from this position',
		contradict: 'the witness heard something else here',
		lost: 'inside a run of 5+ words the witness never heard',
		unheard: 'the witness did not hear this word (a single word, not a run)',
		unmatchable: 'nothing comparable in the transcript (punctuation, vocables)'
	};

	function witnessTitle(w: KaraokeWord): string {
		const at = w.start_s === null || w.start_s === undefined ? 'no timing' : `${w.start_s.toFixed(2)}s`;
		if (w.witness === null || w.witness === undefined) return `${at} - no witness verdict recorded`;
		return `${at} - ${WITNESS_MEANINGS[w.witness] ?? w.witness}`;
	}

	const VERDICT_LABELS: Record<string, string> = {
		vocal: 'Vocal',
		sparse: 'Sparse vocals',
		'no-lyrics': 'No lyrics',
		unknown: 'Unknown'
	};

	/** Takes the server's OPEN string, not the writer union: a verdict the
	 *  daemon starts stamping later renders as itself instead of blanking. */
	function verdictLabel(v: string): string {
		return VERDICT_LABELS[v] ?? v;
	}

	async function setOverride(v: LyricVerdictValue | null): Promise<void> {
		if (data === null) return;
		try {
			const updated = await putLyricOverride(stableId, v);
			lyricsCache.applyVerdict(stableId, updated);
			pushToast(v === null ? 'Override cleared' : `Marked ${verdictLabel(v)}`);
		} catch (exc) {
			pushToast(`Override failed: ${exc}`, 'error');
		}
	}

	function seekTo(w: KaraokeWord): void {
		if (w.start_s === null || w.start_s === undefined || audioEl === null || audioOk !== true) return;
		audioEl.currentTime = w.start_s;
		void audioEl.play();
	}

	$effect(() => {
		const id = stableId;
		const url = audioUrl;
		// untrack: _load reads the shared cache, so a tracked call would
		// re-enter this effect on every cache write and re-probe the audio
		// three times per track. The identity of the track is the only real
		// dependency.
		untrack(() => void _load(id, url));
	});
</script>

<h3>Lyrics</h3>

{#if !loaded}
	<p class="muted">Loading lyrics...</p>
{:else if failure !== null}
	<p class="muted">Lyrics could not be read for this track: {failure}</p>
{:else if data === null}
	<p class="muted">
		No karaoke lyric data for this track yet. Words arrive from the alignment
		pipeline, loaded into state.db with <code>python -m apps.lyrics ingest-state</code>;
		tracks without separated stems are not yet processed. Words produced by the
		branch-era schema need <code>python -m apps.lyrics migrate-legacy-words</code>
		before this panel can see them.
	</p>
{:else}
	{@const v = data.verdict}
	<div class="qc qc-{v.effective_verdict}">
		<b>{verdictLabel(v.effective_verdict)}</b>
		{#if v.override}<span class="badge" title="A human verdict is set and overrides the computed one">your call</span>{/if}
		<span class="sep">·</span>
		<span title="Share of the track's duration where the separated vocal stem carries energy. At or below the calibrated band this track is auto-stamped no-lyrics.">
			{v.coverage_pct === null || v.coverage_pct === undefined
				? 'coverage n/a'
				: `${v.coverage_pct.toFixed(1)}% vocal coverage`}
		</span>
		<span class="sep">·</span>
		<span title="Size of the aligned artifact: words in the flat timeline, and the lines apps/lyrics/lines.py derives from them.">
			{v.n_words ?? data.words.length} words{#if v.n_lines !== null && v.n_lines !== undefined}
				/ {v.n_lines} lines{/if}
		</span>
		<span class="sep">·</span>
		<span title="Where the lyric text came from and how it was matched. An exact get or a licensed matcher get is a strong claim; search and relaxed-title methods are weaker.">
			{v.source ?? 'source unknown'}
		</span>
		{#if v.language_iso3}
			<span class="sep">·</span><span title="Detected language of the lyric text (ISO 639-3)">{v.language_iso3}</span>
		{/if}
	</div>

	<p class="muted small">
		<span title="Words the independent ASR witness contradicts or never heard. Calibrated on ground truth: these carry about 4.5x the base error rate.">
			{suspectCount} of {data.words.length} words suspect
			({v.pct_witness_red === null || v.pct_witness_red === undefined
				? 'n/a'
				: `${(v.pct_witness_red * 100).toFixed(0)}%`})
		</span>
	</p>

	{#if audioOk === true}
		<audio
			bind:this={audioEl}
			src={audioUrl}
			controls
			preload="metadata"
			ontimeupdate={(e) => (currentTime = (e.currentTarget as HTMLAudioElement).currentTime)}
			onerror={() => {
				audioOk = false;
				audioReason = 'the browser could not decode or stream this file';
			}}
		></audio>
	{:else if audioOk === false}
		<p class="muted small">
			Audio for this track could not be played ({audioReason ?? 'reason not recorded'}), so
			the words cannot be followed. The timings below are still the aligner's output.
			This is the library relink problem, not a lyrics one - see docs/library-availability.md.
		</p>
	{/if}

	<p class="words">
		{#each data.words as w (w.idx)}
			<button
				type="button"
				class="w {witnessClass(w)}"
				class:active={w.idx === activeIdx}
				title={witnessTitle(w)}
				onclick={() => seekTo(w)}
			>{w.word}</button>{#if w.line_final}<br />{/if}
		{/each}
	</p>

	<div class="override">
		<span class="muted small">This track really is:</span>
		<button onclick={() => setOverride('vocal')}>Vocal</button>
		<button onclick={() => setOverride('sparse')}>Sparse</button>
		<button onclick={() => setOverride('no-lyrics')}>No lyrics</button>
		{#if v.override}<button onclick={() => setOverride(null)}>clear</button>{/if}
	</div>
{/if}

<style>
	.qc {
		padding: 8px 12px;
		border-radius: 8px;
		border-left: 4px solid var(--muted);
		background: rgba(127, 127, 127, 0.08);
		margin-bottom: 6px;
	}
	.qc-vocal { border-left-color: #39c07c; }
	.qc-sparse { border-left-color: #d9a441; }
	.qc-no-lyrics { border-left-color: #8b90a0; }
	.qc-unknown { border-left-color: #8b90a0; }
	.badge {
		font-size: 0.75rem;
		border: 1px solid currentColor;
		border-radius: 999px;
		padding: 0 6px;
		margin-left: 6px;
	}
	.sep { opacity: 0.4; margin: 0 6px; }
	.muted { color: var(--muted); }
	.small { font-size: 0.85rem; }
	.words {
		line-height: 2;
		max-height: 340px;
		overflow-y: auto;
		padding: 8px;
		border-radius: 8px;
		background: rgba(127, 127, 127, 0.05);
	}
	.w {
		border: 0;
		background: none;
		padding: 1px 3px;
		border-radius: 4px;
		font: inherit;
		cursor: pointer;
		color: inherit;
	}
	.w-good { opacity: 0.95; }
	.w-mid { color: #d9a441; }
	.w-bad { color: #e05252; text-decoration: underline wavy currentColor; }
	.w.active { background: #5b8def; color: #fff; }
	.override { margin-top: 8px; display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
	audio { width: 100%; margin: 6px 0; }
</style>
