<script lang="ts">
	import { onMount, tick } from 'svelte';
	import { triggerFloatingAction } from '$lib/ui/clamp-to-viewport';
	import { parseTempoBpmInput } from '$lib/rb/tempo-edit';
	import {
		canonicalKeyLabel,
		emptyTapTempo,
		formatDraftBpm,
		tapTempoAt,
		type TapTempoState
	} from './bpm-key-cell-edit';

	let {
		kind,
		anchor,
		initial,
		onclose,
		oncommit
	}: {
		kind: 'bpm' | 'key';
		anchor: HTMLElement;
		initial: string;
		onclose: () => void;
		oncommit: (value: number | string) => Promise<void>;
	} = $props();

	// Seeded in onMount so the prop is read inside a closure. The parent keys
	// this popover, so a different cell remounts it rather than reusing the draft.
	let draft = $state('');
	let error = $state<string | null>(null);
	let saving = $state(false);
	let paneEl = $state<HTMLDivElement | null>(null);
	let inputEl = $state<HTMLInputElement | null>(null);
	let taps: TapTempoState = emptyTapTempo();
	let clickCtx: AudioContext | null = null;

	onMount(() => {
		void (async () => {
			draft = initial;
			await tick();
			inputEl?.focus();
			inputEl?.select();
		})();
	});

	function playTapClick(): void {
		const Ctx = window.AudioContext;
		if (Ctx === undefined) return;
		clickCtx ??= new Ctx();
		void clickCtx.resume();
		const now = clickCtx.currentTime;
		const osc = clickCtx.createOscillator();
		const gain = clickCtx.createGain();
		osc.frequency.value = 880;
		osc.type = 'square';
		gain.gain.setValueAtTime(0.0001, now);
		gain.gain.exponentialRampToValueAtTime(0.12, now + 0.005);
		gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.04);
		osc.connect(gain);
		gain.connect(clickCtx.destination);
		osc.start(now);
		osc.stop(now + 0.05);
	}

	function onTap(): void {
		playTapClick();
		const next = tapTempoAt(taps, performance.now());
		taps = next.state;
		if (next.bpm === null) return;
		draft = formatDraftBpm(next.bpm);
		error = null;
	}

	async function commit(): Promise<void> {
		if (saving) return;
		if (kind === 'bpm') {
			const bpm = parseTempoBpmInput(draft);
			if (bpm === null) {
				error = 'Enter a tempo above 0, or tap the beat';
				return;
			}
			saving = true;
			try {
				await oncommit(bpm);
			} finally {
				saving = false;
			}
			return;
		}
		const label = canonicalKeyLabel(draft);
		if (label === null) {
			error = 'Use a Camelot key like 8A, or a note like Gm';
			return;
		}
		saving = true;
		try {
			await oncommit(label);
		} finally {
			saving = false;
		}
	}

	function onKeydown(event: KeyboardEvent): void {
		if (event.key !== 'Escape') return;
		event.preventDefault();
		event.stopPropagation();
		onclose();
	}

	function onInputKeydown(event: KeyboardEvent): void {
		if (event.key !== 'Enter') return;
		event.preventDefault();
		event.stopPropagation();
		void commit();
	}

	function onOutsidePointerDown(event: PointerEvent): void {
		const target = event.target;
		if (!(target instanceof Node)) return;
		if (paneEl !== null && paneEl.contains(target)) return;
		onclose();
	}
</script>

<svelte:window onkeydown={onKeydown} onpointerdowncapture={onOutsidePointerDown} />

<div
	bind:this={paneEl}
	class="bpm-key-edit"
	data-testid="bpm-key-edit"
	role="dialog"
	tabindex="-1"
	aria-label={kind === 'bpm' ? 'Edit tempo' : 'Edit key'}
	use:triggerFloatingAction={{ getTrigger: () => anchor, preferred: 'above', gap: 6 }}
	oncontextmenu={(event) => event.preventDefault()}
>
	<label>
		<span>{kind === 'bpm' ? 'Tempo' : 'Key'}</span>
		<input
			bind:this={inputEl}
			bind:value={draft}
			data-testid="bpm-key-edit-input"
			aria-label={kind === 'bpm' ? 'Tempo BPM' : 'Key'}
			title={kind === 'bpm'
				? 'Tempo in beats per minute: type it, or tap the beat. Kept for this session and sent to a deck playing this track.'
				: 'Key as a Camelot code (8A) or a note name (Gm). Kept for this session and nudged onto a deck playing this track.'}
			onkeydown={onInputKeydown}
		/>
	</label>
	{#if kind === 'bpm'}
		<button
			type="button"
			class="tap"
			data-testid="bpm-key-edit-tap"
			title="Tap at the beat to measure the tempo"
			onclick={onTap}
		>
			Tap
		</button>
	{/if}
	<button type="button" data-testid="bpm-key-edit-set" disabled={saving} onclick={() => void commit()}>
		Set
	</button>
	{#if error !== null}
		<p class="err" role="alert">{error}</p>
	{:else if kind === 'bpm'}
		<p class="hint">Tap the beat to measure it, then Set.</p>
	{/if}
</div>

<style>
	.bpm-key-edit {
		position: fixed;
		z-index: 1100;
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 6px;
		min-width: 180px;
		max-width: calc(100vw - 16px);
		padding: 8px;
		border: 1px solid var(--rb-border);
		border-radius: 4px;
		background: var(--rb-panel-raised);
		box-shadow: 0 5px 18px rgb(0 0 0 / 45%);
		color: var(--rb-text);
		font: inherit;
		font-size: 12px;
	}
	label {
		display: flex;
		align-items: center;
		gap: 6px;
	}
	input {
		width: 72px;
		padding: 2px 4px;
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		background: var(--rb-panel);
		color: var(--rb-text);
		font: inherit;
		font-variant-numeric: tabular-nums;
	}
	button {
		padding: 2px 8px;
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		background: var(--rb-panel);
		color: var(--rb-text);
		font: inherit;
		cursor: pointer;
	}
	button.tap {
		font-weight: 650;
	}
	button:hover {
		background: var(--rb-select);
	}
	button:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
	.hint,
	.err {
		flex: 1 0 100%;
		margin: 0;
		font-size: 11px;
	}
	.hint {
		color: var(--rb-text-dim);
	}
	.err {
		color: var(--rb-bpm-mismatch-ring, #e14238);
	}
</style>
