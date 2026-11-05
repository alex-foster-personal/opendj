<script lang="ts">
	/**
	 * GRID-OCTAVE-02: the hover card on a disputed BPM (library cell and deck
	 * readout). One line names both tempos and which one is shown; one click
	 * stores the other as the track's tempo pick, which the deck grid and the
	 * library BPM then both follow. A refusal is shown in the card and handed
	 * to `onerror`, never swallowed.
	 */
	import { onMount } from 'svelte';
	import { otherAlternative, useAlternativeLabel, bpmDisputeText, type BpmAlternative } from '$lib/rb/bpm-dispute';
	import { placeFloating } from '$lib/ui/clamp-to-viewport';

	let {
		alternatives,
		shownBpm,
		confidence,
		note = null,
		anchor,
		onpick,
		onerror,
		onenter,
		onleave,
		onDismiss
	}: {
		alternatives: BpmAlternative[];
		shownBpm: number | null;
		confidence: number | null | undefined;
		/** Extra detail under the dispute line (the cell's usual BPM explainer). */
		note?: string | null;
		anchor: { left: number; top: number; width: number; height: number };
		onpick: (alt: BpmAlternative) => Promise<void>;
		onerror: (message: string) => void;
		onenter: () => void;
		onleave: () => void;
		onDismiss: () => void;
	} = $props();

	const text = $derived(bpmDisputeText(alternatives, shownBpm, confidence));
	const other = $derived(otherAlternative(alternatives, shownBpm));
	let saving = $state(false);
	let failure = $state<string | null>(null);

	async function pick(): Promise<void> {
		if (other === null || saving) return;
		saving = true;
		failure = null;
		try {
			await onpick(other);
			onDismiss();
		} catch (err) {
			failure = `Could not switch the tempo: ${err instanceof Error ? err.message : String(err)}`;
			onerror(failure);
		} finally {
			saving = false;
		}
	}

	let el = $state<HTMLDivElement | null>(null);
	let cardW = $state(0);
	let cardH = $state(0);
	let winW = $state(0);
	let winH = $state(0);

	$effect(() => {
		const node = el;
		if (node === null || typeof ResizeObserver === 'undefined') return;
		const ro = new ResizeObserver(() => {
			const box = node.getBoundingClientRect();
			cardW = box.width;
			cardH = box.height;
		});
		ro.observe(node);
		return () => ro.disconnect();
	});

	const cardStyle = $derived.by(() => {
		if (cardW <= 0 || cardH <= 0 || winW <= 0 || winH <= 0) return 'visibility:hidden;left:0;top:0';
		const box = placeFloating({
			trigger: { left: anchor.left + anchor.width / 2 - cardW / 2, top: anchor.top, width: cardW, height: anchor.height },
			size: { width: cardW, height: cardH },
			viewport: { width: winW, height: winH },
			preferred: 'above',
			gap: 4
		});
		return `visibility:visible;left:${box.x}px;top:${box.y}px`;
	});

	onMount(() => {
		const dismiss = () => onDismiss();
		window.addEventListener('scroll', dismiss, { capture: true, passive: true });
		return () => window.removeEventListener('scroll', dismiss, { capture: true });
	});
</script>

<svelte:window bind:innerWidth={winW} bind:innerHeight={winH} />

{#if text !== null}
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div
		class="bpm-dispute-card"
		data-hover-card
		data-testid="bpm-dispute-card"
		bind:this={el}
		style={cardStyle}
		onpointerenter={onenter}
		onpointerleave={onleave}
	>
		<div class="bdc-line">{text}</div>
		{#if other !== null}
			<button type="button" class="bdc-use" disabled={saving} onclick={pick} data-testid="bpm-dispute-use-other">
				{saving ? 'Switching...' : useAlternativeLabel(other)}
			</button>
		{/if}
		{#if failure !== null}
			<div class="bdc-failure" role="alert">{failure}</div>
		{/if}
		{#if note}
			<div class="bdc-note">{note}</div>
		{/if}
	</div>
{/if}

<style>
	.bpm-dispute-card {
		position: fixed;
		z-index: 60;
		display: grid;
		gap: 6px;
		max-width: 340px;
		padding: 7px 10px;
		color: var(--rb-text, #c8cdd2);
		background: var(--rb-panel-raised, #20252d);
		border: 1px solid var(--rb-border, #23282f);
		border-radius: 5px;
		box-shadow: 0 6px 18px rgba(0, 0, 0, 0.35);
		font-size: 12px;
		white-space: normal;
	}
	.bdc-use {
		justify-self: start;
		padding: 3px 8px;
		font-size: 11.5px;
		color: var(--rb-text, #c8cdd2);
		background: var(--rb-panel, #181c22);
		border: 1px solid var(--rb-orange, #e8a13a);
		border-radius: 4px;
		cursor: pointer;
	}
	.bdc-use:disabled {
		cursor: default;
		opacity: 0.6;
	}
	.bdc-failure {
		color: var(--rb-red, #e05252);
		font-size: 11px;
	}
	.bdc-note {
		font-size: 10.5px;
		color: var(--rb-text-dim, #838990);
	}
</style>
