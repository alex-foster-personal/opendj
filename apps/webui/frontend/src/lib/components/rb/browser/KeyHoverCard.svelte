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
	import { placeFloating } from '$lib/ui/clamp-to-viewport';

	let {
		ownKey,
		shift,
		shiftedKey,
		note = null,
		anchor,
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
		/** Viewport-space rect of the hovered key cell (from getBoundingClientRect). */
		anchor: { left: number; top: number; width: number; height: number };
		onDismiss: () => void;
	} = $props();

	const shown = $derived(letteredAndCamelot(shift === 0 ? ownKey : shiftedKey));
	const sup = $derived(keyShiftSuperscript(shift));
	const shiftLine = $derived(keyShiftHoverLine(ownKey, shift));

	let el = $state<HTMLDivElement | null>(null);
	let cardW = $state(0);
	let cardH = $state(0);
	let winW = $state(0);
	let winH = $state(0);

	$effect(() => {
		const node = el;
		if (node === null || typeof ResizeObserver === 'undefined') return;
		const ro = new ResizeObserver(() => {
			cardW = node.offsetWidth;
			cardH = node.offsetHeight;
		});
		ro.observe(node);
		return () => ro.disconnect();
	});

	const pos = $derived.by(() => {
		if (cardW <= 0 || cardH <= 0 || winW <= 0 || winH <= 0) {
			return null;
		}
		const box = placeFloating({
			trigger: {
				left: anchor.left + anchor.width / 2 - cardW / 2,
				top: anchor.top,
				width: cardW,
				height: anchor.height
			},
			size: { width: cardW, height: cardH },
			viewport: { width: winW, height: winH },
			preferred: 'above',
			gap: 6
		});
		return { left: box.x, top: box.y };
	});

	const cardStyle = $derived.by(() => {
		if (pos === null) {
			return 'visibility:hidden;left:0;top:0';
		}
		return `visibility:visible;left:${pos.left}px;top:${pos.top}px`;
	});

	onMount(() => {
		// A scroll moves the cell out from under a fixed card: drop it.
		const dismiss = () => onDismiss();
		window.addEventListener('scroll', dismiss, { capture: true, passive: true });
		return () => window.removeEventListener('scroll', dismiss, { capture: true });
	});
</script>

<svelte:window bind:innerWidth={winW} bind:innerHeight={winH} />

{#if note !== null || shown !== null}
	<div class="key-hover-card" bind:this={el} role="tooltip" style={cardStyle}>
		{#if note !== null}
			<div class="khc-cap">{note}</div>
		{:else if shown !== null}
			<div class="khc-pair">
				<span class="khc-letter">{shown.lettered}{#if sup}<sup>{sup}</sup>{/if}</span>
				<span class="khc-camelot">{shown.camelot}{#if sup}<sup>{sup}</sup>{/if}</span>
			</div>
			<div class="khc-cap">{shown.quality}</div>
			{#if shiftLine !== null}
				<div class="khc-shift">{shiftLine}</div>
			{/if}
		{/if}
	</div>
{/if}

<style>
	.key-hover-card {
		position: fixed;
		z-index: 60;
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
