<script lang="ts">
	// SET-10: the REC button's input picker. Inputs are listed BY NAME from the
	// daemon (ffmpeg AVFoundation), and the choice is sent by name so it still
	// means the same input after something else is plugged in. "Tracklist only"
	// is always offered, so REC still records the set when no input can be
	// listed (no ffmpeg on this Mac) or none is wanted.
	import { onMount } from 'svelte';
	import type { RecorderDevices, RecorderStatus } from '../../../../routes/sets/sets-api';
	import {
		getRememberedInput,
		initialInputChoice,
		listRecorderDevices,
		startPerformanceRecorder,
		type RecordInputChoice
	} from '$lib/sets/record-input-choice';

	let {
		onstarted,
		oncancel,
		notify
	}: {
		/** Called with the live status once recording has started. */
		onstarted: (status: RecorderStatus) => void;
		oncancel: () => void;
		/** The rail's toast sink, passed in so this lazy chunk adds no store importer. */
		notify: (message: string, level: 'info' | 'error') => void;
	} = $props();

	let busy = $state(false);

	const NONE_VALUE = '\u0000none';

	let devices = $state<RecorderDevices | null>(null);
	let loading = $state(true);
	let listError = $state<string | null>(null);
	let selected = $state<string | null>(null);
	let startButton = $state<HTMLButtonElement | null>(null);

	const hasLoopback = $derived(devices?.devices.some((d) => d.loopback) ?? false);

	function toValue(choice: RecordInputChoice | null): string | null {
		if (choice === null) return null;
		return choice.kind === 'none' ? NONE_VALUE : choice.name;
	}

	function toChoice(value: string): RecordInputChoice {
		return value === NONE_VALUE ? { kind: 'none' } : { kind: 'device', name: value };
	}

	onMount(() => {
		void (async () => {
			// The remembered choice only preselects, so failing to read it
			// costs nothing but that; failing to list inputs is shown.
			const [listed, remembered] = await Promise.allSettled([
				listRecorderDevices(),
				getRememberedInput()
			]);
			if (listed.status === 'fulfilled') devices = listed.value;
			else listError = listed.reason instanceof Error ? listed.reason.message : String(listed.reason);
			loading = false;
			const last = remembered.status === 'fulfilled' ? remembered.value : null;
			selected = toValue(initialInputChoice(last, devices));
			queueMicrotask(() => startButton?.focus());
		})();
	});

	async function start(): Promise<void> {
		if (selected === null || busy) return;
		const choice = toChoice(selected);
		busy = true;
		try {
			const status = await startPerformanceRecorder(choice);
			const from = choice.kind === 'device' ? `from ${choice.name}` : 'tracklist only';
			notify(`Recording ${status.session_id} (${from})`, 'info');
			onstarted(status);
		} catch (error) {
			notify(`REC failed: ${String(error)}`, 'error');
		} finally {
			busy = false;
		}
	}

	function onKeydown(event: KeyboardEvent): void {
		if (event.key === 'Escape') {
			event.preventDefault();
			oncancel();
		}
	}
</script>

<svelte:window onkeydown={onKeydown} />

<div
	class="rec-backdrop"
	role="dialog"
	aria-modal="true"
	aria-labelledby="rec-picker-title"
	tabindex="-1"
	onpointerdown={(e) => e.target === e.currentTarget && oncancel()}
>
	<form
		class="rec-picker"
		data-testid="record-input-picker"
		aria-labelledby="rec-picker-title"
		onsubmit={(e) => {
			e.preventDefault();
			void start();
		}}
	>
		<h2 id="rec-picker-title">Record set from</h2>
		{#if loading}
			<p class="rec-note">Finding audio inputs...</p>
		{:else}
			{#if listError !== null}
				<p class="rec-error" data-testid="record-input-error">
					Audio inputs could not be listed: {listError}
				</p>
			{/if}
			<div class="rec-options" role="radiogroup" aria-label="Audio input">
				{#each devices?.devices ?? [] as device (device.index)}
					<label class="rec-option">
						<input type="radio" name="rec-input" value={device.name} bind:group={selected} />
						<span class="rec-name">{device.name}</span>
						{#if device.loopback}
							<span class="rec-tag" title="A virtual input that carries computer audio back in, so it records what the decks play">loopback</span>
						{/if}
					</label>
				{/each}
				<label class="rec-option">
					<input type="radio" name="rec-input" value={NONE_VALUE} bind:group={selected} />
					<span class="rec-name">Tracklist only (no audio)</span>
				</label>
			</div>
			{#if devices !== null && !hasLoopback}
				<p class="rec-note">
					No loopback input found. To record exactly what the decks play, install a loopback
					input such as BlackHole and send Open DJ's output through it; a microphone records the room.
				</p>
			{/if}
		{/if}
		<div class="rec-actions">
			<button type="button" onclick={oncancel}>Cancel</button>
			<button
				type="submit"
				class="rec-start"
				bind:this={startButton}
				disabled={loading || busy || selected === null}
				title={selected === null ? 'Pick an input first' : 'Start recording'}
			>
				Start recording
			</button>
		</div>
	</form>
</div>

<style>
	.rec-backdrop {
		position: fixed;
		inset: 0;
		z-index: 1100;
		display: flex;
		align-items: center;
		justify-content: center;
		background: rgb(0 0 0 / 45%);
	}
	.rec-picker {
		width: min(380px, calc(100vw - 32px));
		max-height: calc(100vh - 32px);
		overflow: auto;
		padding: 14px 16px;
		background: var(--rb-panel-raised, #222);
		color: var(--rb-text, #eee);
		border: 1px solid var(--rb-border, #444);
		border-radius: 6px;
		box-shadow: 0 8px 24px rgb(0 0 0 / 50%);
		font-size: 12px;
	}
	h2 {
		margin: 0 0 10px;
		font-size: 13px;
		font-weight: 600;
	}
	.rec-options {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}
	.rec-option {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 5px 6px;
		border-radius: 4px;
		cursor: pointer;
	}
	.rec-option:hover {
		background: var(--rb-panel, #1a1a1a);
	}
	.rec-name {
		flex: 1;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.rec-tag {
		padding: 1px 5px;
		border-radius: 3px;
		background: #1f3a24;
		color: #8fd49a;
		font-size: 10px;
	}
	.rec-note {
		margin: 8px 0 0;
		color: var(--rb-text-dim, #aaa);
		line-height: 1.4;
	}
	.rec-error {
		margin: 0 0 8px;
		color: #f08a80;
		line-height: 1.4;
	}
	.rec-actions {
		display: flex;
		justify-content: flex-end;
		gap: 8px;
		margin-top: 12px;
	}
	.rec-actions button {
		padding: 4px 12px;
		border-radius: 4px;
		border: 1px solid var(--rb-border, #444);
		background: var(--rb-panel, #1a1a1a);
		color: inherit;
		cursor: pointer;
	}
	.rec-actions .rec-start {
		background: #8e2620;
		border-color: #d0342c;
	}
	.rec-actions button:disabled {
		opacity: 0.5;
		cursor: default;
	}
</style>
