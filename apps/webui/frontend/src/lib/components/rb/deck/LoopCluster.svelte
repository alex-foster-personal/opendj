<script lang="ts">
	// Loop cluster (SCREENSHOT-SPEC 3): INT source dropdown (visual-only),
	// big beat-length readout, < > halve/double. Real behaviour via the
	// audio engine: clicking the readout engages/disengages an exact PQTZ
	// beat loop at the current position; halve/double resize it in place.
	// Requires a loaded track with a real beatgrid. Sub-beat loops remain
	// unimplemented until the engine has honest PQTZ interpolation.
	import type { DeckState } from '$lib/rb/types';

	let {
		deck,
		pending,
		onEngage,
		onDisengage,
		inertTip
	}: {
		deck: DeckState;
		pending: boolean;
		onEngage: (beats: number, startMs?: number) => Promise<void>;
		onDisengage: () => Promise<void>;
		inertTip: string;
	} = $props();

	const MIN_BEATS = 1;
	const MAX_BEATS = 512;

	let beatLength: number = $state(4);

	const engaged: boolean = $derived(deck.loop !== null && deck.loop.engaged);
	const gridBeatCount: number = $derived(deck.anlz?.beatgrid.beats.length ?? 0);
	const canToggle: boolean = $derived(
		!pending && deck.stable_id !== null && (engaged || gridBeatCount > beatLength)
	);
	const nextHalved: number = $derived(Math.max(MIN_BEATS, Math.floor(beatLength / 2)));
	const nextDoubled: number = $derived(Math.min(MAX_BEATS, beatLength * 2));
	const canHalve: boolean = $derived(
		!pending && deck.stable_id !== null && gridBeatCount > nextHalved
	);
	const canDouble: boolean = $derived(
		!pending &&
		deck.stable_id !== null &&
		nextDoubled > beatLength &&
		gridBeatCount > nextDoubled
	);
	const disabledTip: string = $derived(
		pending
			? 'deck command pending'
			: deck.stable_id === null
			? 'no track loaded'
			: deck.anlz === null || deck.anlz.beatgrid.beats.length === 0
				? 'track has no beatgrid - beat loop unavailable'
				: deck.anlz.beatgrid.beats.length <= beatLength
					? `beatgrid has too few beats for a ${beatLength}-beat loop`
				: ''
	);

	// ----------------------------------------------------------- _helpers

	function _fmtBeats(n: number): string {
		return String(n);
	}

	async function _reapply(): Promise<void> {
		// Resize a live loop keeping its in point.
		if (!engaged || deck.loop === null) return;
		await onEngage(beatLength, deck.loop.in_ms);
	}

	async function toggleLoop(): Promise<void> {
		if (!canToggle) return;
		if (engaged) {
			await onDisengage();
		} else {
			await onEngage(beatLength);
		}
	}

	async function halve(): Promise<void> {
		if (!canHalve) return;
		beatLength = nextHalved;
		await _reapply();
	}

	async function double(): Promise<void> {
		if (!canDouble) return;
		beatLength = nextDoubled;
		await _reapply();
	}
</script>

<div class="loop-cluster">
	<button class="rb-lit-button rb-inert int" disabled title={inertTip}>
		INT <span class="caret">&#9662;</span>
	</button>
	<button
		class="readout"
		class:engaged
		disabled={!canToggle}
		data-performance-control="loop"
		data-state={engaged ? 'on' : 'off'}
		title={canToggle ? (engaged ? 'exit loop' : `loop ${_fmtBeats(beatLength)} beats`) : disabledTip}
		onclick={toggleLoop}
	>
		{_fmtBeats(beatLength)}
	</button>
	<div class="halve-double">
		<button
			disabled={!canHalve}
			data-performance-control="loop-halve"
			title={canHalve ? 'halve loop length' : disabledTip}
			onclick={halve}
		>
			&lt;
		</button>
		<button
			disabled={!canDouble}
			data-performance-control="loop-double"
			title={canDouble ? 'double loop length' : disabledTip}
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
