<script lang="ts">
	// SET-10 / SET-12: the REC button's source picker, a MACHINE | LOOPBACK |
	// MIDI toggle with one view each. MACHINE (the default) records Open DJ's
	// own master mix, which needs no driver, as every other DJ app does.
	// LOOPBACK lists loopback inputs (BlackHole) and links an installer. MIDI is
	// a hardware mixer or controller whose mix comes back through an audio
	// interface input, so it lists every other input. Inputs are listed BY NAME
	// from the daemon and sent by name, so a choice still means the same input
	// after something else is plugged in. "Tracklist only" is always reachable.
	import { onMount } from 'svelte';
	import type { RecorderDevices, RecorderStatus } from '../../../../routes/sets/sets-api';
	import { getRecorderStatus } from '../../../../routes/sets/sets-api';
	import {
		INSTALL_LOOPBACK_URL,
		RECORD_MODES,
		defaultChoiceForMode,
		devicesForMode,
		getRememberedInput,
		initialRecordSelection,
		listRecorderDevices,
		masterMixUnavailableReason,
		startPerformanceRecorder,
		type RecordInputChoice,
		type RecordMode
	} from '$lib/sets/record-input-choice';
	import { nativeShellKind, openExternal } from '$lib/shell/native-shell';

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

	const NONE_VALUE = '\u0000none';
	const MASTER_VALUE = '\u0000master';
	const masterUnavailable = masterMixUnavailableReason();

	let busy = $state(false);
	let devices = $state<RecorderDevices | null>(null);
	let recordingsDir = $state<string | null>(null);
	let loading = $state(true);
	let listError = $state<string | null>(null);
	let rememberError = $state<string | null>(null);
	let mode = $state<RecordMode>('machine');
	let selected = $state<string | null>(null);
	let startButton = $state<HTMLButtonElement | null>(null);

	const listed = $derived(devicesForMode(devices, mode));

	function toValue(choice: RecordInputChoice | null): string | null {
		if (choice === null) return null;
		if (choice.kind === 'device') return choice.name;
		return choice.kind === 'none' ? NONE_VALUE : MASTER_VALUE;
	}

	function toChoice(value: string): RecordInputChoice {
		if (value === NONE_VALUE) return { kind: 'none' };
		return value === MASTER_VALUE ? { kind: 'master' } : { kind: 'device', name: value };
	}

	function reason(error: unknown): string {
		return error instanceof Error ? error.message : String(error);
	}

	function switchMode(next: RecordMode): void {
		if (next === mode) return;
		mode = next;
		selected = toValue(defaultChoiceForMode(next, devices, masterUnavailable === null));
	}

	async function installLoopback(): Promise<void> {
		try {
			if (nativeShellKind() !== null) await openExternal(INSTALL_LOOPBACK_URL);
			else window.open(INSTALL_LOOPBACK_URL, '_blank', 'noopener');
		} catch (error) {
			notify(`Could not open ${INSTALL_LOOPBACK_URL}: ${reason(error)}`, 'error');
		}
	}

	onMount(() => {
		void (async () => {
			// Each failure is shown: an unreadable remembered input must not look
			// like "nothing remembered yet" (SET-10), and MACHINE needs no listing.
			const [inputs, remembered, status] = await Promise.allSettled([
				listRecorderDevices(),
				getRememberedInput(),
				getRecorderStatus()
			]);
			if (inputs.status === 'fulfilled') devices = inputs.value;
			else listError = reason(inputs.reason);
			if (remembered.status === 'rejected') rememberError = reason(remembered.reason);
			if (status.status === 'fulfilled') recordingsDir = status.value.recordings_dir ?? null;
			loading = false;
			const last = remembered.status === 'fulfilled' ? remembered.value : null;
			const initial = initialRecordSelection(last, devices, masterUnavailable === null);
			mode = initial.mode;
			selected = toValue(initial.choice);
			queueMicrotask(() => startButton?.focus());
		})();
	});

	async function start(): Promise<void> {
		if (selected === null || busy) return;
		const choice = toChoice(selected);
		busy = true;
		try {
			const status = await startPerformanceRecorder(choice, mode);
			const from =
				choice.kind === 'device'
					? `from ${choice.name}`
					: choice.kind === 'master'
						? 'master mix'
						: 'tracklist only';
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
		<div class="rec-modes" role="tablist" aria-label="Recording source">
			{#each RECORD_MODES as entry (entry.mode)}
				<button
					type="button"
					role="tab"
					class:active={mode === entry.mode}
					aria-selected={mode === entry.mode}
					title={entry.title}
					data-testid={`record-mode-${entry.mode}`}
					onclick={() => switchMode(entry.mode)}>{entry.label}</button
				>
			{/each}
		</div>
		{#if loading}
			<p class="rec-note">Finding audio inputs...</p>
		{:else}
			{#if rememberError !== null}
				<p class="rec-error" data-testid="record-input-remember-error">
					The last source used could not be read, so none is preselected: {rememberError}
				</p>
			{/if}
			{#if mode === 'machine'}
				<div class="rec-machine" data-testid="record-mode-machine-view">
					{#if masterUnavailable !== null}
						<p class="rec-error" data-testid="record-master-unavailable">{masterUnavailable}</p>
					{:else}
						<label class="rec-option">
							<input type="radio" name="rec-input" value={MASTER_VALUE} bind:group={selected} />
							<span class="rec-name">Master mix (internal)</span>
						</label>
						<dl class="rec-facts">
							<dt>Captures</dt>
							<dd>What the audience hears: the master output after the master fader. Never the headphone cue.</dd>
							<dt>Format</dt>
							<dd>WAV, 16-bit stereo, at the engine's sample rate, in 5-minute segments</dd>
							<dt>Saved to</dt>
							<dd class="rec-path">{recordingsDir ?? 'the recordings folder'}/&lt;set&gt;/</dd>
						</dl>
						<p class="rec-note">No driver or loopback needed. Keep this page open while recording.</p>
					{/if}
				</div>
			{:else}
				{#if listError !== null}
					<p class="rec-error" data-testid="record-input-error">
						Audio inputs could not be listed: {listError}
					</p>
				{/if}
				{#if mode === 'loopback'}
					{#if devices !== null && listed.length === 0}
						<p class="rec-note" data-testid="record-no-loopback">
							No loopback input is installed on this Mac. You do not need one: MACHINE records the
							master mix directly.
						</p>
					{/if}
					<button
						type="button"
						class="rec-install"
						data-testid="record-install-loopback"
						onclick={() => void installLoopback()}>Install loopback</button
					>
				{:else}
					<p class="rec-note">
						Hardware mixer or controller: pick the audio interface input its mix comes back on.
					</p>
				{/if}
				<div class="rec-options" role="radiogroup" aria-label="Audio input">
					{#each listed as device (device.index)}
						<label class="rec-option">
							<input type="radio" name="rec-input" value={device.name} bind:group={selected} />
							<span class="rec-name">{device.name}</span>
							{#if device.loopback}
								<span class="rec-tag" title="A virtual input that carries computer audio back in">loopback</span>
							{/if}
						</label>
					{/each}
					<label class="rec-option">
						<input type="radio" name="rec-input" value={NONE_VALUE} bind:group={selected} />
						<span class="rec-name">Tracklist only (no audio)</span>
					</label>
				</div>
			{/if}
		{/if}
		<div class="rec-actions">
			<button type="button" onclick={oncancel}>Cancel</button>
			<button
				type="submit"
				class="rec-start"
				bind:this={startButton}
				disabled={loading || busy || selected === null}
				title={selected === null ? 'Pick a source first' : 'Start recording'}
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
		width: min(400px, calc(100vw - 32px));
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
	.rec-modes {
		display: flex;
		margin-bottom: 10px;
		border: 1px solid var(--rb-border, #444);
		border-radius: 4px;
		overflow: hidden;
	}
	.rec-modes button {
		flex: 1;
		padding: 5px 0;
		border: 0;
		background: var(--rb-panel, #1a1a1a);
		color: var(--rb-text-dim, #aaa);
		font-size: 11px;
		font-weight: 600;
		letter-spacing: 0.06em;
		cursor: pointer;
	}
	.rec-modes button + button {
		border-left: 1px solid var(--rb-border, #444);
	}
	.rec-modes button.active {
		background: #8e2620;
		color: #fff;
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
	.rec-facts {
		display: grid;
		grid-template-columns: auto 1fr;
		gap: 4px 10px;
		margin: 8px 6px 0;
	}
	.rec-facts dt {
		color: var(--rb-text-dim, #aaa);
	}
	.rec-facts dd {
		margin: 0;
	}
	.rec-path {
		overflow-wrap: anywhere;
		font-family: ui-monospace, monospace;
		font-size: 11px;
	}
	.rec-install {
		margin: 8px 0;
		padding: 4px 12px;
		border-radius: 4px;
		border: 1px solid var(--rb-border, #444);
		background: var(--rb-panel, #1a1a1a);
		color: inherit;
		cursor: pointer;
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
