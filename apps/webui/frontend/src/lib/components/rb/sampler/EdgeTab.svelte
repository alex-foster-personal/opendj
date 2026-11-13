<script lang="ts">
	/**
	 * The inside of one filled edge tab (spec 7.4, mock variant D `.wslot`): the pin dot and the
	 * 428 px row. DOM order is play, repeat, title + meta, waveform, pencil: EdgeTabControls owns
	 * the four triggers and renders the title and meta between them; CSS orders the row per
	 * side so play sits at the screen edge. EdgeWave owns the card element, its width, its
	 * --reveal / --treveal fades and the pin click.
	 */
	import { highlightRanges } from '$lib/player/sampler/edge-search';
	import { sampler } from '$lib/player/sampler/sampler-store.svelte';
	import {
		bpmIsCompatible,
		cueLabel,
		keyPlanFor,
		stemsPending
	} from '$lib/player/sampler/sampler-store-logic';
	import { displayName, type Edge, type SamplerSlot } from '$lib/player/sampler/sampler-types';
	import { uiPrefs } from '$lib/rb/prefs.svelte';
	import EdgeTabControls from './EdgeTabControls.svelte';

	let { edge, slot, guard }: { edge: Edge; slot: SamplerSlot; guard: () => boolean } = $props();

	const title = $derived(displayName(slot));
	const segments = $derived.by(() => {
		const out: Array<{ t: string; m: boolean }> = [];
		let at = 0;
		for (const [a, b] of highlightRanges(title, sampler.edges[edge].query)) {
			if (a > at) out.push({ t: title.slice(at, a), m: false });
			out.push({ t: title.slice(a, b), m: true });
			at = b;
		}
		if (at < title.length) out.push({ t: title.slice(at), m: false });
		return out;
	});
	const plan = $derived(keyPlanFor(slot, sampler.master, uiPrefs.compatible_filter));
	const bpmOk = $derived(bpmIsCompatible(slot, sampler.master, uiPrefs.compatible_filter));
	const bpmTip = $derived(
		bpmOk
			? 'Tempo of this sample as captured, in BPM'
			: 'Tempo of this sample as captured, in BPM: outside the compatible tempo range of the master deck'
	);
	const ratioPct = $derived.by(() => {
		const bpm = sampler.master?.bpm;
		if (bpm == null || !(slot.native_bpm > 0)) return '';
		const r = bpm / slot.native_bpm;
		return `${r >= 1 ? '+' : ''}${((r - 1) * 100).toFixed(1)}%`;
	});
	const isMix = $derived(slot.vector !== null || slot.source === 'capture');
</script>

<i class="pinmark"></i>
<div class="in">
	<EdgeTabControls {edge} {slot} {guard}>
		<div class="txt">
			<div class="lb">
				{#each segments as s, i (i)}{#if s.m}<mark>{s.t}</mark>{:else}{s.t}{/if}{/each}
			</div>
			<div class="ft">
				<span class="cue">{cueLabel(slot)}</span>
				<span class="k c{plan.dist}"
					>{plan.key ?? '-'}{#if plan.shift}<sup>{plan.shift > 0 ? '+' : ''}{plan.shift}</sup
						>{/if}</span
				>
				<span class="bpm" class:out={!bpmOk} title={bpmTip}>{slot.native_bpm.toFixed(1)}</span>
				<span class="r">{ratioPct}</span>
				{#if isMix && stemsPending(slot)}<span
						class="kept"
						data-testid="edge-tab-stems-pending"
						title="Stems pending: this track has no stem-set yet, so it plays the full mix with its EQ and filter. The stem settings apply once its stems exist."
						>MIX . stems pending</span
					>{:else if isMix}<span
						class="kept"
						title="Plays with the EQ, filter and stem settings it was captured with">MIX</span
					>{/if}
			</div>
		</div>
	</EdgeTabControls>
</div>

<style>
	.pinmark {
		position: absolute;
		top: 5px;
		width: 7px;
		height: 7px;
		border-radius: 50%;
		background: var(--es-fg);
		display: none;
	}
	:global(.edge-wave.R) .pinmark {
		left: 12px;
	}
	:global(.edge-wave.L) .pinmark {
		right: 12px;
	}
	:global(.wslot.pinned) > .pinmark {
		display: block;
	}
	.in {
		position: absolute;
		top: 0;
		bottom: 0;
		width: 428px;
		display: grid;
		grid-template-columns: 24px minmax(0, 1fr) 22px 100px 28px;
		column-gap: 8px;
		align-items: center;
		font-family: var(--es-font-body);
		color: var(--es-fg);
		opacity: var(--reveal, 0);
	}
	:global(.edge-wave.R) .in {
		right: 12px;
	}
	:global(.edge-wave.L) .in {
		left: 12px;
		grid-template-columns: 28px 100px 22px minmax(0, 1fr) 24px;
	}
	:global(.edge-wave.R) .txt {
		order: 2;
	}
	:global(.edge-wave.L) .txt {
		order: 4;
	}
	.txt {
		min-width: 0;
		display: grid;
		gap: 2px;
		opacity: var(--treveal, 0);
	}
	.lb {
		font-size: 13px;
		font-weight: 500;
		line-height: 1.2;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.lb mark {
		background: var(--es-warn);
		color: var(--es-mark-ink);
		border-radius: 2px;
		padding: 0 1px;
	}
	.ft {
		display: flex;
		gap: 7px;
		font-family: var(--es-font-mono);
		font-size: 11.5px;
		line-height: 1.2;
		color: var(--es-muted);
		white-space: nowrap;
		overflow: hidden;
	}
	.k.c0 {
		color: var(--es-ok);
	}
	.k.c1 {
		color: var(--es-warn);
	}
	.k.c2 {
		color: var(--es-dim);
	}
	.ft sup {
		font-size: 9px;
		color: var(--es-deck-a);
	}
	.bpm.out {
		color: var(--es-bad);
	}
	.kept {
		color: var(--es-deck-a);
		font-family: var(--es-font-mono);
		font-size: 10.5px;
		border: 1px solid var(--es-mix-border);
		border-radius: 3px;
		padding: 0 3px;
		line-height: 1.3;
	}
	:global(.wslot.playing) .lb {
		color: var(--es-accent);
	}
	:global(.wslot.dim) .in {
		opacity: 0.3;
	}
</style>
