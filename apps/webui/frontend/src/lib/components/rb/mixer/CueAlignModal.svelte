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
	import { deriveAlignment, estimatedCalibrationSeconds } from '$lib/player/cue-align-policy';
	import {
		closeCueAlignModal,
		continueCueAlignment,
		cueAlignmentRunning,
		startCueAlignment
	} from '$lib/rb/cue-align-session.svelte';
	import {
		audioInterference,
		audioInterferenceWarning,
		refreshAudioInterference
	} from '$lib/rb/audio-interference.svelte';
	import type { HeadphoneAlignmentMode, HeadphoneState } from '$lib/rb/mixer-types';

	let { headphones, onmode }: { headphones: HeadphoneState; onmode: (mode: HeadphoneAlignmentMode) => void } =
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

	// Asked as soon as the modal is on screen: the warning is worth most BEFORE
	// the operator spends a run on it, and again on Start in case they quit the
	// offending app in between.
	void refreshAudioInterference();

	/** CUEOUT-21: other software that listens to the mic can take it mid-run.
	 * Advisory only: it never blocks a run and says nothing when the engine
	 * could not look. */
	const interferenceWarning = $derived(audioInterferenceWarning(audioInterference.report));

	const estimatedSeconds = estimatedCalibrationSeconds();
	const calibration = $derived(headphones.calibration);
	/** Set once Continue is pressed so the button cannot fire twice in one step. */
	let cueCheckReleased = $state(false);
	/** A start that was refused before the machine ran (already running, no graph). */
	let startError = $state<string | null>(null);

	$effect(() => {
		if (calibration.step !== 'mic_check_cue') cueCheckReleased = false;
	});

	/** Stage one, live: which rung the ramp is on and how close it is. The bar
	 * is what lets the operator move the ear cup and SEE the number rise instead
	 * of waiting a whole ramp for a verdict. */
	const probe = $derived(calibration.probe);
	const probePct = $derived(probe === null ? 0 : Math.min(100, Math.round((probe.best / probe.threshold) * 100)));

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
						: calibration.step === 'verifying'
							? 5
							: 0
	);

	function start(): void {
		startError = null;
		void refreshAudioInterference();
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

<div class="ca-overlay" role="presentation" onclick={closeCueAlignModal}>
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
	<div
		class="ca-panel"
		role="dialog"
		aria-modal="true"
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
						speakers, then through the headphones, and the built-in microphone times both. It takes up to
						{estimatedSeconds} seconds; playing decks are paused while it runs and resume when it finishes.
					</p>
				{:else if calibration.step === 'applied' && plan !== null}
					<p class="ca-ok" role="status">
						Applied. Room {calibration.master_latency_ms} ms, headphones {calibration.cue_latency_ms} ms,
						offset {calibration.offset_ms} ms:
						HEAD DELAY {plan.head_delay_ms} ms, ROOM +{plan.master_delay_ms} ms.
						{#if calibration.verify_residual_ms !== null}
							verified: {calibration.verify_residual_ms} ms apart after the fix.
						{/if}
					</p>
					{#if plan.warning !== null}
						<p class="ca-warn" role="status">{plan.warning}</p>
					{/if}
				{:else if calibration.step === 'failed'}
					<p class="ca-error" role="alert">{calibration.error}</p>
				{/if}
				{#if interferenceWarning !== null}
					<p class="ca-warn" role="status">{interferenceWarning}</p>
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
					<li class:active={stepIndex === 4} class:done={stepIndex > 4}>Measuring</li>
					<li class:active={stepIndex === 5}>Verifying</li>
				</ol>
				{#if calibration.step === 'mic_access'}
					<p>Waiting for microphone access. Allow the built-in microphone if the browser asks.</p>
				{:else if calibration.step === 'mic_check_master'}
					<p>
						Chirping through the speakers, getting louder until the microphone hears it. Keep the room quiet
						for a moment.
					</p>
					{@render levelFind()}
				{:else if calibration.step === 'mic_check_cue'}
					<p>
						Speakers heard at {calibration.master_latency_ms} ms.
						Hold one ear cup against the laptop microphone, then press Continue. The chirp gets louder until
						the microphone hears it, so move the cup closer while the bar is short.
					</p>
					{@render levelFind()}
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
				{:else if calibration.step === 'verifying'}
					<p>
						Verifying: one more chirp through each output to confirm the delay fix landed. Keep the ear cup on
						the microphone.
					</p>
					{@render levelFind()}
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

{#snippet levelFind()}
	{#if probe !== null}
		<div class="ca-level" data-cue-align-probe-bus={probe.bus}>
			<div class="ca-level-bar"><span style:width={`${probePct}%`} class:heard={probe.best >= probe.threshold}
				></span></div>
			<span class="ca-level-read">
				level {probe.gain.toFixed(2)}, heard {probe.best.toFixed(2)} of {probe.threshold} needed
			</span>
		</div>
	{/if}
{/snippet}

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
	.ca-level {
		display: flex;
		align-items: center;
		gap: 8px;
		margin: 0 0 10px;
	}
	.ca-level-bar {
		flex: 1;
		height: 6px;
		background: var(--rb-panel, #14171b);
		border: 1px solid var(--rb-border, #333);
		border-radius: 3px;
		overflow: hidden;
	}
	.ca-level-bar span {
		display: block;
		height: 100%;
		background: var(--rb-warn, #e6a23c);
		transition: width 120ms linear;
	}
	.ca-level-bar span.heard {
		background: var(--rb-accent, #4fb3ff);
	}
	.ca-level-read {
		color: var(--rb-text-dim, #999);
		font-size: 11px;
		white-space: nowrap;
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
