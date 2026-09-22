<script lang="ts">
	/**
	 * CUEOUT-14: the cue alignment calibration modal.
	 *
	 * Shell copied from EditSuiteModal (overlay, Escape closes, click outside
	 * closes) at 720px so the step copy and the result table fit on one line.
	 * Every step is rendered from `headphones.calibration`, the SAME object the IPC
	 * mirror publishes, so what the operator sees is what GET /headphones says.
	 * Start and Continue are the modal's own transitions; the alignment mode
	 * radios go through `onmode` (the `headphone_alignment_mode` command) like
	 * every other mixer control.
	 */
	import { deriveAlignment, estimatedCalibrationSeconds } from '$lib/player/cue-align.svelte';
	import {
		closeCueAlignModal,
		continueCueAlignment,
		cueAlignmentRunning,
		startCueAlignment
	} from '$lib/rb/cue-align-session.svelte';
	import type { HeadphoneAlignmentMode, HeadphoneState } from '$lib/rb/mixer-types';

	let { headphones, onmode, embedded = false }: { headphones: HeadphoneState; onmode: (mode: HeadphoneAlignmentMode) => void; embedded?: boolean } =
		$props();

	const MODE_COPY: Record<HeadphoneAlignmentMode, { label: string; detail: string }> = {
		headphones_only: {
			label: 'Headphones only',
			detail: 'Only HEAD DELAY moves. If the phones are behind the room nothing is delayed and a warning stays on the cluster.'
		},
		delay_all: {
			label: 'Delay the room',
			detail: 'The room (MASTER) is delayed until it lands with the phones, up to 1500 ms. The waveform lags with it.'
		},
		hybrid: {
			label: 'Hybrid (recommended)',
			detail: 'Phones ahead: HEAD DELAY. Phones behind: ROOM delay. Whichever side is late waits for the other.'
		}
	};

	const estimatedSeconds = estimatedCalibrationSeconds();
	const calibration = $derived(headphones.calibration);
	/** Set once Continue is pressed so the button cannot fire twice in one step. */
	let cueCheckReleased = $state(false);
	/** A start that was refused before the machine ran (already running, no graph). */
	let startError = $state<string | null>(null);

	$effect(() => {
		if (calibration.step !== 'mic_check_cue') cueCheckReleased = false;
	});

	const plan = $derived(
		calibration.step === 'applied' && calibration.offset_ms !== null
			? deriveAlignment(headphones.alignment_mode, calibration.offset_ms)
			: null
	);

	const stepIndex = $derived(
		calibration.step === 'mic_access'
			? 1
			: calibration.step === 'mic_check_master'
				? 2
				: calibration.step === 'mic_check_cue'
					? 3
					: calibration.step === 'measuring'
						? 4
						: 0
	);

	function start(): void {
		startError = null;
		startCueAlignment({ interactive: true }).catch((error: unknown) => {
			// The machine already wrote `calibration.error` for a failed run; this
			// only catches a start that never reached it.
			if (calibration.step !== 'failed') startError = error instanceof Error ? error.message : String(error);
		});
	}

	function continueCueCheck(): void {
		cueCheckReleased = true;
		continueCueAlignment();
	}

	function onOverlayKeydown(e: KeyboardEvent): void {
		if (e.key === 'Escape') closeCueAlignModal();
	}
</script>

<svelte:window onkeydown={onOverlayKeydown} />

<div class="ca-overlay" class:embedded role="presentation" onclick={() => { if (!embedded) closeCueAlignModal(); }}>
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
	<div
		class="ca-panel"
		role="dialog"
		aria-modal={!embedded}
		aria-label="Cue alignment calibration"
		tabindex="-1"
		data-cue-align-step={calibration.step}
		onclick={(e) => e.stopPropagation()}
	>
		<div class="ca-header">
			<h2>Cue alignment calibration</h2>
			<button class="ca-close" onclick={closeCueAlignModal} aria-label="close">x</button>
		</div>
		<div class="ca-body">
			{#if calibration.step === 'idle' || calibration.step === 'applied' || calibration.step === 'failed'}
				{#if calibration.step === 'idle'}
					<p>
						This measures how far your headphones lag the room. The laptop plays a short chirp through the
						speakers, then through the headphones, and the built-in microphone times both. It takes about
						{estimatedSeconds} seconds; playing decks are paused while it runs and resume when it finishes.
					</p>
				{:else if calibration.step === 'applied' && plan !== null}
					<p class="ca-ok" role="status">
						Applied. Room {calibration.master_latency_ms} ms, headphones {calibration.cue_latency_ms} ms,
						offset {calibration.offset_ms} ms:
						HEAD DELAY {plan.head_delay_ms} ms, ROOM +{plan.master_delay_ms} ms.
					</p>
					{#if plan.warning !== null}
						<p class="ca-warn" role="status">{plan.warning}</p>
					{/if}
				{:else if calibration.step === 'failed'}
					<p class="ca-error" role="alert">{calibration.error}</p>
				{/if}
				{#if startError !== null}
					<p class="ca-error" role="alert">{startError}</p>
				{/if}
				<fieldset class="ca-modes">
					<legend>Alignment mode</legend>
					<!-- One literal radio per mode (the source guard reads the value attributes). -->
					<label class="ca-mode">
						<input
							type="radio"
							name="cue-align-mode"
							value="headphones_only"
							checked={headphones.alignment_mode === 'headphones_only'}
							onchange={() => onmode('headphones_only')}
						/>
						{@render modeCopy('headphones_only')}
					</label>
					<label class="ca-mode">
						<input
							type="radio"
							name="cue-align-mode"
							value="delay_all"
							checked={headphones.alignment_mode === 'delay_all'}
							onchange={() => onmode('delay_all')}
						/>
						{@render modeCopy('delay_all')}
					</label>
					<label class="ca-mode">
						<input
							type="radio"
							name="cue-align-mode"
							value="hybrid"
							checked={headphones.alignment_mode === 'hybrid'}
							onchange={() => onmode('hybrid')}
						/>
						{@render modeCopy('hybrid')}
					</label>
				</fieldset>
				<div class="ca-actions">
					<button
						type="button"
						class="ca-primary"
						disabled={cueAlignmentRunning()}
						onclick={start}>{calibration.step === 'idle' ? 'Start calibration' : 'Run again'}</button
					>
					<button type="button" onclick={closeCueAlignModal}>{calibration.step === 'applied' ? 'Done' : 'Cancel'}</button>
				</div>
			{:else}
				<ol class="ca-steps">
					<li class:active={stepIndex === 1} class:done={stepIndex > 1}>Microphone access</li>
					<li class:active={stepIndex === 2} class:done={stepIndex > 2}>Speakers check</li>
					<li class:active={stepIndex === 3} class:done={stepIndex > 3}>Headphones check</li>
					<li class:active={stepIndex === 4}>Measuring</li>
				</ol>
				{#if calibration.step === 'mic_access'}
					<p>Waiting for microphone access. Allow the built-in microphone if the browser asks.</p>
				{:else if calibration.step === 'mic_check_master'}
					<p>Chirping through the speakers. Keep the room quiet for a moment.</p>
				{:else if calibration.step === 'mic_check_cue'}
					<p>
						Speakers heard at {calibration.master_latency_ms} ms.
						Hold one ear cup against the laptop microphone, then press Continue.
					</p>
					<div class="ca-actions">
						<button type="button" class="ca-primary" disabled={cueCheckReleased} onclick={continueCueCheck}
							>Continue</button
						>
						<button type="button" onclick={closeCueAlignModal}>Abort</button>
					</div>
				{:else if calibration.step === 'measuring'}
					<p>
						Measuring: two more chirps per output so each latency is the median of three. Keep the ear cup on
						the microphone.
					</p>
				{/if}
				{#if calibration.step !== 'mic_check_cue'}
					<div class="ca-actions">
						<button type="button" onclick={closeCueAlignModal}>Abort</button>
					</div>
				{/if}
			{/if}
		</div>
	</div>
</div>

{#snippet modeCopy(mode: HeadphoneAlignmentMode)}
	<span class="ca-mode-label">{MODE_COPY[mode].label}</span>
	<span class="ca-mode-detail">{MODE_COPY[mode].detail}</span>
{/snippet}

<style>
	.ca-overlay {
		position: fixed;
		inset: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		background: rgba(0, 0, 0, 0.55);
		z-index: 200;
	}
	.ca-overlay.embedded {
		position: static;
		display: block;
		background: transparent;
	}
	.ca-panel {
		width: min(720px, 96vw);
		max-height: 82vh;
		display: flex;
		flex-direction: column;
		background: var(--rb-panel-raised, #1c1f24);
		border: 1px solid var(--rb-border, #333);
		border-radius: 4px;
		color: var(--rb-text, #ddd);
		font-family: var(--rb-font, inherit);
	}
	.embedded .ca-panel {
		width: 100%;
		max-height: none;
	}
	.ca-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 10px 14px;
		border-bottom: 1px solid var(--rb-border, #333);
	}
	.ca-header h2 {
		margin: 0;
		font-size: 13px;
		font-weight: 600;
	}
	.ca-close {
		background: transparent;
		border: none;
		color: var(--rb-text-dim, #999);
		font-size: 14px;
		cursor: pointer;
		padding: 2px 6px;
	}
	.ca-close:hover {
		color: var(--rb-text, #ddd);
	}
	.ca-body {
		padding: 12px 14px;
		overflow: auto;
		font-size: 12px;
		line-height: 1.45;
	}
	.ca-body p {
		margin: 0 0 10px;
	}
	.ca-ok {
		color: var(--rb-accent, #4fb3ff);
	}
	.ca-warn {
		color: var(--rb-warn, #e6a23c);
	}
	.ca-error {
		color: var(--rb-danger, #ff6b6b);
	}
	.ca-modes {
		border: 1px solid var(--rb-border, #333);
		border-radius: 3px;
		padding: 8px 10px;
		margin: 0 0 10px;
	}
	.ca-modes legend {
		font-size: 11px;
		color: var(--rb-text-dim, #999);
		padding: 0 4px;
	}
	.ca-mode {
		display: grid;
		grid-template-columns: auto auto 1fr;
		gap: 4px 8px;
		align-items: baseline;
		padding: 3px 0;
		cursor: pointer;
	}
	.ca-mode-label {
		font-weight: 600;
	}
	.ca-mode-detail {
		color: var(--rb-text-dim, #999);
	}
	.ca-steps {
		display: flex;
		gap: 12px;
		list-style: none;
		padding: 0;
		margin: 0 0 10px;
		font-size: 11px;
		color: var(--rb-text-dim, #999);
	}
	.ca-steps li.active {
		color: var(--rb-accent, #4fb3ff);
	}
	.ca-steps li.done {
		color: var(--rb-text, #ddd);
	}
	.ca-actions {
		display: flex;
		gap: 8px;
		justify-content: flex-end;
	}
	.ca-actions button {
		font: inherit;
		font-size: 11px;
		padding: 4px 10px;
		background: var(--rb-panel, #14171b);
		border: 1px solid var(--rb-border, #333);
		border-radius: 3px;
		color: var(--rb-text, #ddd);
		cursor: pointer;
	}
	.ca-actions button.ca-primary {
		border-color: var(--rb-accent, #4fb3ff);
	}
	.ca-actions button:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}
</style>
