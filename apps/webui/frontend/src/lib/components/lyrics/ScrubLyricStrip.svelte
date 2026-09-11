<script lang="ts">
	// Floating word readout for scrub-hover surfaces (PreviewStrip rows and
	// the deck StripWaveform). Pure presentation: the parent resolves the
	// pointer via $lib/lyrics/pointer-word and passes the result in; this
	// component just draws the focus word bold with its neighbours dimmed,
	// following the pointer horizontally. No debounce here on purpose - the
	// readout must update instantly; only the initial LOAD is debounced
	// (lyrics-cache hoverLoadLyrics).
	//
	// pointer-events: none throughout: the readout sits exactly where the
	// DJ is pointing and must never steal the click underneath it.
	//
	// Parent contract: position:relative container with overflow visible;
	// this element anchors above it (bottom: 100%).
	import { readoutWords, type PointerWord, type WordIndex } from '$lib/lyrics/pointer-word';

	let {
		index,
		state,
		xPx,
		widthPx,
		radius = 2
	}: {
		/** The indexed words of the hovered track. */
		index: WordIndex;
		/** The resolved pointer state for the current pointer x. */
		state: PointerWord;
		/** Pointer x in CSS px, relative to the strip's left edge. */
		xPx: number;
		/** Strip width in CSS px (clamps the readout inside the row). */
		widthPx: number;
		/** Display-order neighbours shown either side of the focus word. */
		radius?: number;
	} = $props();

	const words = $derived(readoutWords(index, state, radius));
	const clampedX = $derived(Math.min(widthPx - 8, Math.max(8, xPx)));

	const title = $derived.by(() => {
		if (state.kind === 'inside') {
			return 'word under the pointer (aligned onset/offset seconds bound it)';
		} else if (state.kind === 'before') {
			return 'pointer is just before this word';
		} else if (state.kind === 'after') {
			return 'pointer is just after this word';
		} else {
			return 'pointer is in an instrumental gap between the dimmed words';
		}
	});
</script>

{#if words.length > 0}
	<div
		class="scrub-readout"
		data-kind={state.kind}
		style={`left:${clampedX}px`}
		{title}
		aria-hidden="true"
	>
		{#each words as rw (rw.idx)}<span class="w" data-role={rw.role}>{rw.text}</span>{#if rw.gapAfter}<span
				class="gap-dot"
				title="the pointer is in this gap"
			></span>{/if}{' '}{/each}
	</div>
{/if}

<style>
	.scrub-readout {
		position: absolute;
		bottom: calc(100% + 3px);
		transform: translateX(-50%);
		z-index: 30;
		max-width: 340px;
		padding: 2px 7px 3px;
		border-radius: 4px;
		background: rgba(10, 13, 17, 0.94);
		border: 1px solid var(--rb-border, #3d4652);
		box-shadow: 0 3px 10px rgba(0, 0, 0, 0.5);
		color: #cfd8e2;
		font-size: 12px;
		line-height: 1.3;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		pointer-events: none;
	}
	.w {
		color: #8b97a3;
	}
	.w[data-role='focus'] {
		color: #fff;
		font-weight: 700;
	}
	.scrub-readout[data-kind='inside'] .w[data-role='focus'] {
		color: #ffb340;
	}
	.scrub-readout[data-kind='before'] .w[data-role='focus'] {
		color: #38d6ff;
	}
	.scrub-readout[data-kind='after'] .w[data-role='focus'] {
		color: #b98cff;
	}
	.gap-dot {
		display: inline-block;
		width: 2px;
		height: 0.95em;
		vertical-align: -0.12em;
		margin: 0 2px;
		background: #7c8794;
		box-shadow: 0 0 4px #7c8794;
	}
</style>
