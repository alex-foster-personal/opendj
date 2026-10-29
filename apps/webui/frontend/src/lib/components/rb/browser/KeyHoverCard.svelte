<script lang="ts">
	/**
	 * Key hover card for the library Key column (KEYSHIFT-04, the maintainer, Tue 13 Oct
	 * 2026): the lettered key on the left, the Camelot key just to the right.
	 * A row that matches the compatible reference only after a key shift shows
	 * the key it will play in with the shift as a superscript, and a line with
	 * its own key. One card for the whole table, fixed-positioned, so the
	 * scrolling table body cannot clip it.
	 */
	import { onMount } from 'svelte';
	import {
		keyShiftHoverLine,
		keyShiftSuperscript,
		letteredAndCamelot
	} from '$lib/rb/compatible-filter-copy';

	let {
		ownKey,
		shift,
		shiftedKey,
		note = null,
		x,
		y,
		onDismiss
	}: {
		/** The row's own key as stored. */
		ownKey: string | null;
		/** Load shift in semitones; 0 when it matches (or is shown) as it is. */
		shift: number;
		/** The row's key after `shift`, Camelot notation. */
		shiftedKey: string | null;
		/** Shown instead of the key pair when the key failed or is missing. */
		note?: string | null;
		/** Viewport x of the cell's center and y of its top edge. */
		x: number;
		y: number;
		onDismiss: () => void;
	} = $props();

	const shown = $derived(letteredAndCamelot(shift === 0 ? ownKey : shiftedKey));
	const sup = $derived(keyShiftSuperscript(shift));
	const shiftLine = $derived(keyShiftHoverLine(ownKey, shift));

	onMount(() => {
		// A scroll moves the cell out from under a fixed card: drop it.
		const dismiss = () => onDismiss();
		window.addEventListener('scroll', dismiss, { capture: true, passive: true });
		return () => window.removeEventListener('scroll', dismiss, { capture: true });
	});
</script>

{#if note !== null}
	<div class="key-hover-card" role="tooltip" style={`left:${x}px;top:${y}px`}>
		<div class="khc-cap">{note}</div>
	</div>
{:else if shown !== null}
	<div class="key-hover-card" role="tooltip" style={`left:${x}px;top:${y}px`}>
		<div class="khc-pair">
			<span class="khc-letter">{shown.lettered}{#if sup}<sup>{sup}</sup>{/if}</span>
			<span class="khc-camelot">{shown.camelot}{#if sup}<sup>{sup}</sup>{/if}</span>
		</div>
		<div class="khc-cap">{shown.quality}</div>
		{#if shiftLine !== null}
			<div class="khc-shift">{shiftLine}</div>
		{/if}
	</div>
{/if}

<style>
	.key-hover-card {
		position: fixed;
		z-index: 60;
		transform: translate(-50%, calc(-100% - 6px));
		display: grid;
		gap: 4px;
		padding: 6px 9px;
		white-space: nowrap;
		pointer-events: none;
		color: var(--rb-text, #c8cdd2);
		background: var(--rb-panel-raised, #20252d);
		border: 1px solid var(--rb-border, #23282f);
		border-radius: 5px;
		box-shadow: 0 6px 18px rgba(0, 0, 0, 0.35);
		font-size: 12px;
	}
	.khc-pair {
		display: grid;
		grid-template-columns: auto auto;
		gap: 0 14px;
		align-items: baseline;
	}
	.khc-letter {
		font-weight: 600;
		font-size: 13.5px;
		text-align: right;
	}
	.khc-camelot {
		font-family: ui-monospace, Menlo, monospace;
		font-size: 12.5px;
		color: var(--rb-text-dim, #838990);
	}
	.khc-cap {
		font-size: 10.5px;
		color: var(--rb-text-dim, #838990);
	}
	.khc-shift {
		font-size: 11px;
		color: var(--rb-orange, #e8a13a);
	}
	sup {
		font-size: 0.7em;
		font-weight: 600;
		color: var(--rb-orange, #e8a13a);
	}
</style>
