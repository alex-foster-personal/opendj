<script lang="ts">
	// Loop cluster (SCREENSHOT-SPEC 3): INT source dropdown (visual-only),
	// big beat-length readout, < > halve/double. Real behaviour via the
	// audio engine: clicking the readout engages/disengages a beat loop at
	// the current position; halve/double resize a live loop in place.
	// Requires a loaded track WITH a BPM - otherwise disabled (fail-fast,
	// no guessed beat lengths).
	import type { DeckState } from '$lib/rb/types';

	let {
		deck,
		onEngage,
		onDisengage,
		inertTip
	}: {
		deck: DeckState;
		onEngage: (in_ms: number, out_ms: number) => void;
		onDisengage: () => void;
		inertTip: string;
	} = $props();

	const MIN_BEATS = 0.125;
	const MAX_BEATS = 512;

	let beatLength: number = $state(4);

	const engaged: boolean = $derived(deck.loop !== null && deck.loop.engaged);
	const canLoop: boolean = $derived(deck.stable_id !== null && deck.bpm !== null);
	const disabledTip: string = $derived(
		deck.stable_id === null
			? 'no track loaded'
			: deck.bpm === null
				? 'track has no BPM - beat loop unavailable'
				: ''
	);

	// ----------------------------------------------------------- _helpers

	function _beatMs(): number {
		if (deck.bpm === null) throw new Error('LoopCluster: beat maths need a BPM');
		return 60000 / deck.bpm;
	}

	function _fmtBeats(n: number): string {
		if (n >= 1) return String(n);
		return `1/${Math.round(1 / n)}`;
	}

	function _reapply(): void {
		// Resize a live loop keeping its in point.
		if (!engaged || deck.loop === null) return;
		onEngage(deck.loop.in_ms, deck.loop.in_ms + beatLength * _beatMs());
	}

	function toggleLoop(): void {
		if (!canLoop) return;
		if (engaged) {
			onDisengage();
		} else {
			const in_ms = deck.position_ms;
			onEngage(in_ms, in_ms + beatLength * _beatMs());
		}
	}

	function halve(): void {
		if (!canLoop) return;
		beatLength = Math.max(MIN_BEATS, beatLength / 2);
		_reapply();
	}

	function double(): void {
		if (!canLoop) return;
		beatLength = Math.min(MAX_BEATS, beatLength * 2);
		_reapply();
	}
</script>

<div class="loop-cluster">
	<button class="rb-lit-button rb-inert int" disabled title={inertTip}>
		INT <span class="caret">&#9662;</span>
	</button>
	<button
		class="readout"
		class:engaged
		disabled={!canLoop}
		title={canLoop ? (engaged ? 'exit loop' : `loop ${_fmtBeats(beatLength)} beats`) : disabledTip}
		onclick={toggleLoop}
	>
		{_fmtBeats(beatLength)}
	</button>
	<div class="halve-double">
		<button disabled={!canLoop} title={canLoop ? 'halve loop length' : disabledTip} onclick={halve}>
			&lt;
		</button>
		<button
			disabled={!canLoop}
			title={canLoop ? 'double loop length' : disabledTip}
			onclick={double}
		>
			&gt;
		</button>
	</div>
</div>

<style>
	.loop-cluster {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 3px;
		flex: 0 0 auto;
	}
	.int {
		width: 100%;
		text-align: center;
	}
	.caret {
		color: var(--rb-text-dim);
	}
	.readout {
		width: 44px;
		background: #080a0d;
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: 20px;
		font-weight: 600;
		font-variant-numeric: tabular-nums;
		text-align: center;
		padding: 2px 0;
		cursor: pointer;
	}
	.readout.engaged {
		color: var(--rb-orange);
		border-color: var(--rb-orange);
		box-shadow: 0 0 5px rgba(232, 161, 58, 0.5);
	}
	.readout:disabled {
		color: var(--rb-text-dim);
		cursor: default;
		opacity: 0.6;
	}
	.halve-double {
		display: flex;
		gap: 2px;
		width: 100%;
	}
	.halve-double button {
		flex: 1;
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
		padding: 1px 0;
		cursor: pointer;
	}
	.halve-double button:disabled {
		color: var(--rb-text-dim);
		cursor: default;
		opacity: 0.6;
	}
</style>
