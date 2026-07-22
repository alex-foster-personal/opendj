<script lang="ts">
	/**
	 * One mixer channel strip (SCREENSHOT-SPEC 4), top-down:
	 * channel number, TRIM / HIGH / MID / LOW knobs (real -> engine),
	 * headphone CUE button routed through the shared cue-bus dispatcher,
	 * vertical channel fader (real -> engine), STEM label (static).
	 */
	import type { DeckId, EqBand } from '$lib/rb/types';
	import Knob from './Knob.svelte';
	import VFader from './VFader.svelte';

	interface Props {
		/** The deck this strip controls (screen order is 3 1 2 4). */
		deckId: DeckId;
		/** TRIM knob 0..1; 0.5 = unity. */
		trim: number;
		/** HIGH knob 0..1; 0.5 = flat. */
		eqHigh: number;
		/** MID knob 0..1; 0.5 = flat. */
		eqMid: number;
		/** LOW knob 0..1; 0.5 = flat. */
		eqLow: number;
		/** Channel fader 0..1; 1 = full. */
		fader: number;
		cueEnabled: boolean;
		ontrim: (value: number) => void;
		oneq: (band: EqBand, value: number) => void;
		onfader: (value: number) => void;
		oncue: (enabled: boolean) => void;
	}

	let { deckId, trim, eqHigh, eqMid, eqLow, fader, cueEnabled, ontrim, oneq, onfader, oncue }: Props = $props();
</script>

<div class="strip" data-mixer-channel={deckId}>
	<span class="ch-num">{deckId}</span>
	<Knob label="TRIM" value={trim} onchange={ontrim} />
	<Knob label="HIGH" value={eqHigh} onchange={(v) => oneq('high', v)} />
	<Knob label="MID" value={eqMid} onchange={(v) => oneq('mid', v)} />
	<Knob label="LOW" value={eqLow} onchange={(v) => oneq('low', v)} />
	<button class:enabled={cueEnabled} class="cue-btn" aria-pressed={cueEnabled} onclick={() => oncue(!cueEnabled)}>CUE</button>
	<VFader value={fader} onchange={onfader} label={`channel ${deckId} fader`} />
	<span class="stem-label">STEM</span>
</div>

<style>
	.strip {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: space-between;
		gap: 3px;
		min-height: 0;
	}
	.ch-num {
		font-size: 10px;
		color: var(--rb-text);
		line-height: 1;
	}
	.cue-btn {
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text-dim);
		font-family: var(--rb-font);
		font-size: 8px;
		letter-spacing: 0.04em;
		padding: 2px 5px;
		line-height: 1;
	}
	.cue-btn.enabled { color: var(--rb-accent); border-color: var(--rb-accent); }
	.stem-label {
		font-size: 8px;
		letter-spacing: 0.06em;
		color: var(--rb-text-dim);
		line-height: 1;
	}
</style>
