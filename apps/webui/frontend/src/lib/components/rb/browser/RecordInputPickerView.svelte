<script module lang="ts">
	/** Radio values for the two non-device choices; a device's value is its name. */
	export const NONE_VALUE = '\u0000none';
	export const MASTER_VALUE = '\u0000master';
</script>

<script lang="ts">
	// RecordInputPicker's dialog as props only: the container lists inputs,
	// remembers the last one and starts the recorder; the selected radio and
	// the start button are bound back to it.
	import type { RECORD_MODES, RecordMode } from '$lib/sets/record-input-choice';
	import type { RecorderDevices } from '../../../../routes/sets/sets-api';

	let {
		modes,
		mode,
		selected = $bindable(null),
		startButton = $bindable(null),
		loading,
		busy,
		devicesLoaded,
		listed,
		listError,
		rememberError,
		masterUnavailable,
		recordingsDir,
		onswitchmode,
		oninstall,
		onstart,
		oncancel
	}: {
		modes: typeof RECORD_MODES;
		mode: RecordMode;
		/** The checked radio's value: a device name, NONE_VALUE or MASTER_VALUE. */
		selected?: string | null;
		startButton?: HTMLButtonElement | null;
		loading: boolean;
		busy: boolean;
		/** False until the input listing answered (the no-loopback note waits on it). */
		devicesLoaded: boolean;
		/** The inputs this mode lists. */
		listed: RecorderDevices['devices'];
		listError: string | null;
		rememberError: string | null;
		/** Why MACHINE cannot record here, or null when it can. */
		masterUnavailable: string | null;
		recordingsDir: string | null;
		onswitchmode: (mode: RecordMode) => void;
		oninstall: () => void;
		onstart: () => void;
		oncancel: () => void;
	} = $props();

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
			onstart();
		}}
	>
		<h2 id="rec-picker-title">Record set from</h2>
		<div class="rec-modes" role="tablist" aria-label="Recording source">
			{#each modes as entry (entry.mode)}
				<button
					type="button"
					role="tab"
					class:active={mode === entry.mode}
					aria-selected={mode === entry.mode}
					title={entry.title}
					data-testid={`record-mode-${entry.mode}`}
					onclick={() => onswitchmode(entry.mode)}>{entry.label}</button
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
					{#if devicesLoaded && listed.length === 0}
						<p class="rec-note" data-testid="record-no-loopback">
							No loopback input is installed on this Mac. You do not need one: MACHINE records the
							master mix directly.
						</p>
					{/if}
					<button
						type="button"
						class="rec-install"
						data-testid="record-install-loopback"
						onclick={() => oninstall()}>Install loopback</button
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
