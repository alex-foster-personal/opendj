<script lang="ts">
	/**
	 * Headphone MIX + GAIN knobs with headphone icon (SCREENSHOT-SPEC 4).
	 * Real CUE/MASTER monitor mix, level, and browser-selected output device.
	 */
	import { knobId } from '$lib/rb/knob-control.svelte';
	import {
		committedMixDirection,
		stepHeadphoneMix,
		type HeadphoneMixDirection,
		type HeadphoneMixStepResult
	} from '$lib/rb/headphone-mix-step';
	import { headphoneLivenessAlertText, headphoneMixAccent, twoOutputsWarning } from '$lib/player/headphones';
	import { calibrateButtonEnabled } from '$lib/player/cue-align-policy';
	import ControlExplainer from '../deck/ControlExplainer.svelte';
	import { ioHoverSummary, ioShouldAlert, outputsUnset } from '$lib/rb/io-outputs-button';
	import { ioOutputsSession, noteSetOutputsClicked } from '$lib/rb/io-outputs-session.svelte';
	import Knob from './Knob.svelte';
	import type { HeadphoneOutputMode, HeadphoneState } from '$lib/rb/mixer-types';
	import type { LivenessVerdict } from '$lib/rb/audio-output-liveness';

	interface Props {
		state: HeadphoneState;
		onmix: (value: number) => void;
		onlevel: (value: number) => void;
		ondelay: (value: number) => void;
		onrefresh: () => void;
		onacquire: () => void;
		onselect: (deviceId: string) => void;
		onmaster: (deviceId: string) => void;
		oninput: (deviceId: string) => void;
		onmode: (mode: HeadphoneOutputMode) => void;
		/** CUEOUT-14: open the mic calibration modal. */
		oncalibrate: () => void;
	}

	let { state, onmix, onlevel, ondelay, onrefresh, onacquire, onselect, onmaster, oninput, onmode, oncalibrate }: Props =
		$props();

	let mixStepDirection: HeadphoneMixDirection = 1;
	let lastMixStep: HeadphoneMixStepResult | null = null;

	function handleMixSingleClick(): void {
		// onmix can be rejected without telling us, so the previous step's direction
		// is only adopted if MIX actually landed on the value that step asked for.
		mixStepDirection = committedMixDirection(mixStepDirection, lastMixStep, state.mix);
		lastMixStep = stepHeadphoneMix(state.mix, mixStepDirection);
		onmix(lastMixStep.value);
	}

	/** CUEOUT-14: live only in two outputs with a selected cue sink; the label is not consulted. */
	const calibrateEnabled = $derived(
		calibrateButtonEnabled({
			output_mode: state.output_mode,
			selected_output_device_id: state.selected_output_device_id
		})
	);

	const modeLabel = $derived(
		state.output_mode === 'practice'
			? 'practice'
			: state.output_mode === 'two_outputs'
				? 'two outputs'
				: 'split cable'
	);

	const selectedLabel = $derived(
		state.selected_output_device_id === null
			? null
			: (state.outputs.find((output) => output.id === state.selected_output_device_id)?.label ?? null)
	);
	const masterLabel = $derived(
		state.selected_master_output_device_id === null
			? null
			: (state.outputs.find((output) => output.id === state.selected_master_output_device_id)?.label ?? null)
	);
	const warningText = $derived(twoOutputsWarning({ outputMode: state.output_mode, selectedLabel }));
	const livenessVerdict = $derived(
		(state as HeadphoneState & { liveness_verdict?: LivenessVerdict }).liveness_verdict ?? 'idle'
	);
	const livenessAlert = $derived(headphoneLivenessAlertText(livenessVerdict));
	const mixBullets = [
		'Turn MIX left: more channel CUE in the blend. Turn right: more MASTER.',
		'Single-click the knob to step toward the other extreme.',
		'MAIN (practice): master stays full on speakers; MIX blends cue on top. Two outputs: MIX is headphones only.',
		'Left is full CUE (orange). Right is full MASTER (blue). Default is full CUE.'
	];
	const levelBullets = [
		'Headphone GAIN (Mixxx Head Gain). Scales the CUE path: the phones in two outputs, the cue ear in SPLIT, and the cue blend in MAIN.',
		'It does not change the room MASTER volume. Default is 1 (full). Turn down if the phones are hot.'
	];
	const mainBullets = [
		'1) Press MAIN for laptop or a single output (practice mode).',
		'2) Turn CUE on for each channel you want in the headphone blend.',
		'3) With no HEADPHONE CUE device picked, MIX left adds more cue into the speaker mix; master stays full.',
		'4) Picking HEADPHONE CUE in I/O switches to two outputs: room on MASTER/MAIN, cue on the phones.'
	];
	const splitBullets = [
		'1) Press SPLIT for a DJ splitter cable: mono master on LEFT, mono cue on RIGHT.',
		'2) Set MIX and GAIN after choosing SPLIT; a Y cable will not separate the legs.',
		'3) Turn CUE on for channels you want on the right ear; master is always the left leg.'
	];
	// Pin 894af5672c3b: the I/O hover is compact (one sentence, the devices in
	// use, the call to action) and dismisses instantly; the per-picker detail
	// lives on each picker's own explainer inside the click menu.
	const ioSummary = $derived(ioHoverSummary(state));
	const ioBullets = $derived([ioSummary.sentence, ...ioSummary.devices, ioSummary.cta]);
	// SET OUTPUTS pulses and glows red while no output is chosen, until the
	// first click this session (sessionStorage; a blocked store falls back to
	// the module's in-memory flag).
	const ioAlert = $derived(ioShouldAlert(outputsUnset(state), ioOutputsSession.clicked));

	function handleSetOutputs(): void {
		noteSetOutputsClicked();
		onacquire();
	}
	const delayBullets = [
		'Mixxx Head Delay, 0-500 ms, on the cue path only. It does not delay the room.',
		'CALIBRATE fills it from the measured offset when the phones are ahead of the room; type a value to override.',
		'Two independently clocked devices still drift. Bluetooth is for auditioning, not beatmatching.'
	];
	const calibrateBullets = [
		'Measures how far the headphones lag the room with the built-in mic and splits the difference between HEAD DELAY and ROOM per the alignment mode.',
		'Live only in two outputs with a HEADPHONE CUE sink selected. Playing decks pause for the chirps and resume after.'
	];
	const roomBullets = [
		'ROOM is the room (MASTER) delay, 0-1500 ms, the last node before the speakers. The phones never pay it.',
		'The waveform and PLAY light lag by the same amount on purpose, so what you see is what the room hears.'
	];
	const rescanBullets = [
		'Re-enumerate outputs and inputs without flipping a Bluetooth headset to HFP.',
		'Use it after plugging in or pairing a device so it shows up in the pickers below.',
		'I/O briefly uses the built-in mic so device names appear. It does not flip Bluetooth to HFP.'
	];
	const modeBullets = [
		'practice (MAIN): one output; enable channel CUE and use MIX to blend cue with full master on speakers.',
		'two outputs: pin MASTER/MAIN for the room and HEADPHONE CUE for phones; cue does not bleed into the room.',
		'split cable (SPLIT): one stereo jack; left = master, right = cue. Requires a DJ splitter, not a Y cable.'
	];
	const sinksBullets = [
		'M is the pinned MASTER/MAIN room sink. C is the HEADPHONE CUE sink.',
		'Change them from I/O. Re-selecting the live sink is a no-op so the room does not glitch.',
		'Unplug, dead battery, or power-back-on of CUE never re-sets MASTER. The room speaker line cannot be interrupted.'
	];
	const masterPickBullets = [
		'Room mix. Pin this to speakers so OS-default headphones cannot steal the room. CUE unplug or reconnect never moves this sink.'
	];
	const cuePickBullets = [
		'Headphone CUE sink: wired or Bluetooth. Press CALIBRATE afterwards to time it against the room. Live Bluetooth pairing is CUEOUT-12, not this control.'
	];
	const inputPickBullets = [
		'Used to unlock output names and by CALIBRATE to time the chirps. Never pick a headphone/HFP mic.'
	];

	function stepHeadDelay(delta: number): void {
		const next = Math.min(500, Math.max(0, state.head_delay_ms + delta));
		ondelay(next);
	}

	function toggleSplit(): void {
		if (state.output_mode === 'split_cable') {
			onmode(state.selected_output_device_id !== null ? 'two_outputs' : 'practice');
		} else {
			onmode('split_cable');
		}
	}
</script>

<div class="hp" data-performance-control="headphones">
	<svg class="hp-icon" width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
		<!-- headphone band + ear cups -->
		<path
			d="M2 8 V6 a4 4 0 0 1 8 0 v2"
			fill="none"
			stroke="currentColor"
			stroke-width="1.2"
		/>
		<rect x="1" y="7" width="2.4" height="3.4" rx="0.8" fill="currentColor" />
		<rect x="8.6" y="7" width="2.4" height="3.4" rx="0.8" fill="currentColor" />
	</svg>
	<ControlExplainer title="MIX" bullets={mixBullets} demo="headphone-mix" showDelayMs={60}>
		<Knob
			knobId={knobId('hp', 'hp-mix')}
			label="MIX"
			accessibleLabel="Headphone CUE to MASTER mix"
			value={state.mix}
			onchange={onmix}
			onsingleclick={handleMixSingleClick}
			resetValue={0}
			accentColor={headphoneMixAccent(state.mix)}
		/>
	</ControlExplainer>
	<ControlExplainer title="GAIN" bullets={levelBullets} showDelayMs={60}>
		<Knob
			knobId={knobId('hp', 'hp-level')}
			label="GAIN"
			accessibleLabel="Headphone cue gain"
			value={state.level}
			onchange={onlevel}
		/>
	</ControlExplainer>
	<ControlExplainer title="Output mode" bullets={modeBullets} demo="headphone-mode" showDelayMs={60}>
		<span class="hp-mode" data-output-mode={state.output_mode}>{modeLabel}</span>
	</ControlExplainer>
	<!-- Pin 894af5672c3b: MAIN above SPLIT, beside a double-height SET OUTPUTS. -->
	<div class="hp-mode-stack" data-hp-mode-stack>
		<ControlExplainer title="MAIN" bullets={mainBullets} demo="headphone-practice" showDelayMs={60}>
			<button
				type="button"
				class="hp-btn"
				aria-pressed={state.output_mode === 'practice'}
				aria-label="Practice output mode"
				onclick={() => onmode('practice')}>MAIN</button
			>
		</ControlExplainer>
		<ControlExplainer title="SPLIT" bullets={splitBullets} demo="headphone-split" showDelayMs={60}>
			<button
				type="button"
				class="hp-btn"
				aria-pressed={state.output_mode === 'split_cable'}
				aria-label="Split cable output mode"
				onclick={toggleSplit}>SPLIT</button
			>
		</ControlExplainer>
	</div>
	<ControlExplainer
		title="Audio I/O"
		bullets={ioBullets}
		pinOnClick={true}
		dismiss="instant"
		action={outputMenu}
	>
		<button
			type="button"
			class="hp-btn hp-btn-io"
			class:io-alert={ioAlert}
			data-io-alert={ioAlert ? 'unset' : undefined}
			aria-label="SHOW AUDIO I/O"
			aria-expanded={state.supported}
			onclick={handleSetOutputs}><span>SET</span><span>OUTPUTS</span></button
		>
	</ControlExplainer>
	{#if masterLabel !== null || selectedLabel !== null}
		<ControlExplainer title="Pinned sinks" bullets={sinksBullets} showDelayMs={60}>
			<span class="hp-sinks">
				{#if masterLabel !== null}<span>M {masterLabel}</span>{/if}
				{#if selectedLabel !== null}<span>C {selectedLabel}</span>{/if}
			</span>
		</ControlExplainer>
	{/if}
	{#if state.output_mode === 'split_cable'}
		<ControlExplainer title="SPLIT warning" bullets={splitBullets} showDelayMs={60}>
			<span class="hp-split-warn">
				Room feed is mono. Use a DJ splitter cable, not a Y cable.
			</span>
		</ControlExplainer>
	{/if}
	<ControlExplainer title="CALIBRATE" bullets={calibrateBullets} showDelayMs={60}>
		<button
			type="button"
			class="hp-btn"
			aria-label="CALIBRATE CUE ALIGNMENT"
			data-performance-control="cue-calibrate"
			disabled={!calibrateEnabled}
			onclick={oncalibrate}>CALIBRATE</button
		>
	</ControlExplainer>
	{#if state.output_mode === 'two_outputs'}
		<ControlExplainer title="HEAD DELAY" bullets={delayBullets} showDelayMs={60}>
			<div class="hp-delay" data-performance-control="head-delay">
				<span class="hp-delay-label">HEAD DELAY</span>
				<span class="hp-delay-stepper" role="group" aria-label="head delay stepper">
					<button type="button" class="hp-delay-step" aria-label="increase head delay" onclick={() => stepHeadDelay(1)}>
						<svg viewBox="0 0 10 6" width="10" height="6" aria-hidden="true">
							<path d="M1 5 L5 1 L9 5" fill="none" stroke="currentColor" stroke-width="1.4" />
						</svg>
					</button>
					<button type="button" class="hp-delay-step" aria-label="decrease head delay" onclick={() => stepHeadDelay(-1)}>
						<svg viewBox="0 0 10 6" width="10" height="6" aria-hidden="true">
							<path d="M1 1 L5 5 L9 1" fill="none" stroke="currentColor" stroke-width="1.4" />
						</svg>
					</button>
				</span>
				<span class="hp-delay-value" title={`${state.head_delay_ms} ms head delay on the cue path`}>{state.head_delay_ms}</span>
				<span class="hp-delay-unit">ms</span>
			</div>
		</ControlExplainer>
		{#if state.master_delay_ms > 0}
			<ControlExplainer title="ROOM" bullets={roomBullets} showDelayMs={60}>
				<span class="hp-room" data-performance-control="room-delay">ROOM +{state.master_delay_ms} ms</span>
			</ControlExplainer>
		{/if}
		{#if warningText !== null}
			<ControlExplainer title="Two outputs warning" bullets={[warningText]} showDelayMs={60}>
				<span class="hp-warn" role="status" data-two-outputs-warning>{warningText}</span>
			</ControlExplainer>
		{/if}
		{#if livenessAlert !== null}
			<ControlExplainer title="Headphone output health" bullets={[livenessAlert]} showDelayMs={60}>
				<span class="hp-liveness-alert" role="alert" data-headphone-liveness-alert>{livenessAlert}</span>
			</ControlExplainer>
		{/if}
	{/if}
	{#if state.error !== null}<span class="hp-error">{state.error}</span>{/if}
</div>

{#snippet outputMenu()}
	<div class="hp-menu">
		<!-- Pin d529a7e80a4e: the rescan button and its explainer live in this menu, not the row. -->
		<ControlExplainer title="Rescan" bullets={rescanBullets} showDelayMs={40} placement="right">
			<button
				type="button"
				class="hp-btn hp-rescan"
				aria-label="Rescan available headphone output devices"
				onclick={onrefresh}>↻ RESCAN DEVICES</button
			>
		</ControlExplainer>
		<ControlExplainer title="MASTER / MAIN" bullets={masterPickBullets} showDelayMs={40} placement="right">
			<label class="hp-pick">
				<span>MASTER / MAIN</span>
				<select
					aria-label="master output device"
					value={state.selected_master_output_device_id ?? ''}
					disabled={!state.supported}
					onchange={(event) => onmaster(event.currentTarget.value)}
				>
					<option value="" disabled>choose master</option>
					{#each state.outputs as output (output.id)}
						<option value={output.id}>{output.label || output.id}</option>
					{/each}
				</select>
			</label>
		</ControlExplainer>
		<ControlExplainer title="HEADPHONE CUE" bullets={cuePickBullets} showDelayMs={40} placement="right">
			<label class="hp-pick">
				<span>HEADPHONE CUE</span>
				<select
					aria-label="headphone output device"
					value={state.selected_output_device_id ?? ''}
					disabled={!state.supported}
					onchange={(event) => onselect(event.currentTarget.value)}
				>
					<option value="" disabled>HP out</option>
					{#each state.outputs as output (output.id)}
						<option value={output.id}>{output.label || output.id}</option>
					{/each}
				</select>
			</label>
		</ControlExplainer>
		<ControlExplainer title="AUDIO IN" bullets={inputPickBullets} showDelayMs={40} placement="right">
			<label class="hp-pick">
				<span>AUDIO IN</span>
				<select
					aria-label="audio input device"
					value={state.selected_input_device_id ?? ''}
					onchange={(event) => oninput(event.currentTarget.value)}
				>
					<option value="" disabled>choose input</option>
					{#each state.inputs as input (input.id)}
						<option value={input.id}>{input.label || input.id}</option>
					{/each}
				</select>
			</label>
		</ControlExplainer>
	</div>
{/snippet}

<style>
	.hp {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 4px;
		min-width: 0;
	}
	.hp-icon {
		color: var(--rb-text-dim);
		flex: 0 0 auto;
	}
	.hp-btn {
		font: inherit;
		font-size: 7px;
		letter-spacing: 0.04em;
		padding: 1px 4px;
		line-height: 1.2;
		max-width: 42px;
		background: var(--rb-panel-raised, #1a1e25);
		border: 1px solid var(--rb-border, #23282f);
		border-radius: 2px;
		color: var(--rb-text-dim, #838990);
		cursor: pointer;
	}
	.hp-mode-stack {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}
	.hp-btn-io {
		display: inline-flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		min-height: 23px;
		line-height: 1.1;
	}
	.hp-btn-io.io-alert {
		color: var(--rb-red, #e5484d);
		border-color: var(--rb-red, #e5484d);
		box-shadow: 0 0 6px color-mix(in srgb, var(--rb-red, #e5484d) 70%, transparent);
		animation: io-alert-pulse 1.2s ease-in-out infinite;
	}
	@keyframes io-alert-pulse {
		0%,
		100% {
			box-shadow: 0 0 3px color-mix(in srgb, var(--rb-red, #e5484d) 45%, transparent);
		}
		50% {
			box-shadow: 0 0 9px color-mix(in srgb, var(--rb-red, #e5484d) 95%, transparent);
		}
	}
	@media (prefers-reduced-motion: reduce) {
		/* Glow without pulse. */
		.hp-btn-io.io-alert {
			animation: none;
		}
	}
	.hp-rescan {
		max-width: none;
		align-self: flex-start;
		font-size: 8px;
		padding: 2px 6px;
	}
	.hp-btn:hover {
		color: var(--rb-text, #c8cdd2);
		border-color: var(--rb-accent, #2f6fd6);
	}
	select {
		font: inherit;
		font-size: 7px;
		max-width: 72px;
		padding: 0 2px;
		background: var(--rb-panel-raised, #1a1e25);
		border: 1px solid var(--rb-border, #23282f);
		color: var(--rb-text-dim, #838990);
	}
	.hp-menu {
		display: flex;
		flex-direction: column;
		gap: 6px;
		min-width: 180px;
	}
	.hp-pick {
		display: flex;
		flex-direction: column;
		gap: 2px;
		font-size: 8px;
		letter-spacing: 0.04em;
		color: var(--rb-text-dim, #838990);
	}
	.hp-pick select {
		max-width: none;
		font-size: 10px;
	}
	.hp-sinks {
		display: inline-flex;
		gap: 4px;
		font-size: 7px;
		color: var(--rb-text-dim, #838990);
		max-width: 120px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.hp-mode {
		font-size: 7px;
		letter-spacing: 0.04em;
		color: var(--rb-text-dim, #838990);
		white-space: nowrap;
	}
	.hp-split-warn {
		font-size: 7px;
		color: var(--rb-warn, #e6a23c);
		max-width: 120px;
		line-height: 1.2;
	}
	.hp-error {
		color: var(--rb-danger, #ff6b6b);
		font-size: 7px;
		max-width: 100px;
	}
	.hp-delay {
		display: inline-flex;
		align-items: center;
		gap: 3px;
		font-size: 7px;
		color: var(--rb-text-dim, #838990);
	}
	.hp-delay-label {
		letter-spacing: 0.04em;
	}
	.hp-delay-stepper {
		display: inline-flex;
		align-items: center;
		gap: 1px;
	}
	.hp-delay-step {
		font: inherit;
		line-height: 0;
		padding: 1px 2px;
		background: var(--rb-panel-raised, #1a1e25);
		border: 1px solid var(--rb-border, #23282f);
		border-radius: 2px;
		color: var(--rb-text-dim, #838990);
		cursor: pointer;
	}
	.hp-delay-step:hover {
		color: var(--rb-text, #c8cdd2);
		border-color: var(--rb-accent, #2f6fd6);
	}
	.hp-delay-value {
		min-width: 24px;
		text-align: right;
		font-variant-numeric: tabular-nums;
	}
	.hp-delay-unit {
		letter-spacing: 0.04em;
	}
	.hp-btn:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}
	.hp-room {
		font-size: 7px;
		letter-spacing: 0.04em;
		color: var(--rb-accent, #4fb3ff);
		white-space: nowrap;
	}
	.hp-warn {
		color: var(--rb-warn, #e6a23c);
		font-size: 7px;
		max-width: 140px;
	}
	.hp-liveness-alert {
		color: var(--rb-danger, #ff6b6b);
		font-size: 7px;
		font-weight: 600;
		max-width: 140px;
		line-height: 1.2;
	}
</style>
