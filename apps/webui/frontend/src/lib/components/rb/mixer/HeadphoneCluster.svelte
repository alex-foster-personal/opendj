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
	import { headphoneLivenessAlertText, headphoneMixAccent, monitorLabelIsBluetooth, twoOutputsWarning } from '$lib/player/headphones';
	import { calibrateButtonEnabled } from '$lib/player/cue-align.svelte';
	import { closeCueAlignModal, cueAlignModal } from '$lib/rb/cue-align-session.svelte';
	import ControlExplainer from '../deck/ControlExplainer.svelte';
	import CueAlignModal from './CueAlignModal.svelte';
	import Knob from './Knob.svelte';
	import type { HeadphoneAlignmentMode, HeadphoneOutputMode, HeadphoneState } from '$lib/rb/mixer-types';
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
		onAlignmentMode: (mode: HeadphoneAlignmentMode) => void;
	}

	let { state: headphoneState, onmix, onlevel, ondelay, onrefresh, onacquire, onselect, onmaster, oninput, onmode, oncalibrate, onAlignmentMode }: Props =
		$props();
	let ioOpen = $state(false);
	let ioAcquisitionRequested = false;

	let mixStepDirection: HeadphoneMixDirection = 1;
	let lastMixStep: HeadphoneMixStepResult | null = null;

	function handleMixSingleClick(): void {
		// onmix can be rejected without telling us, so the previous step's direction
		// is only adopted if MIX actually landed on the value that step asked for.
		mixStepDirection = committedMixDirection(mixStepDirection, lastMixStep, headphoneState.mix);
		lastMixStep = stepHeadphoneMix(headphoneState.mix, mixStepDirection);
		onmix(lastMixStep.value);
	}

	/** CUEOUT-14: live only in two outputs with a selected cue sink; the label is not consulted. */
	const calibrateEnabled = $derived(
		calibrateButtonEnabled({
			output_mode: headphoneState.output_mode,
			selected_output_device_id: headphoneState.selected_output_device_id
		})
	);

	const modeLabel = $derived(
		headphoneState.output_mode === 'practice'
			? 'practice'
			: headphoneState.output_mode === 'two_outputs'
				? 'two outputs'
				: 'split cable'
	);

	const selectedLabel = $derived(
		headphoneState.selected_output_device_id === null
			? null
			: (headphoneState.outputs.find((output) => output.id === headphoneState.selected_output_device_id)?.label ?? null)
	);
	const masterLabel = $derived(
		headphoneState.selected_master_output_device_id === null
			? null
			: (headphoneState.outputs.find((output) => output.id === headphoneState.selected_master_output_device_id)?.label ?? null)
	);
	const warningText = $derived(twoOutputsWarning({ outputMode: headphoneState.output_mode, selectedLabel }));
	const livenessVerdict = $derived(
		(headphoneState as HeadphoneState & { liveness_verdict?: LivenessVerdict }).liveness_verdict ?? 'idle'
	);
	const livenessAlert = $derived(headphoneLivenessAlertText(livenessVerdict));
	type SignalReading = {
		state: 'inactive' | 'unavailable' | 'measured';
		rms: number | null;
		peak: number | null;
		measured_at: string | null;
		source: 'application_bus' | 'captured_input';
		physical_output_proven: false;
	};
	const signals = $derived(
		(headphoneState as HeadphoneState & { signals?: Record<'master' | 'cue' | 'input', SignalReading> }).signals
	);
	const mixBullets = [
		'Left is full CUE (orange). Right is full MASTER (blue). Default is full CUE.',
		'In MAIN, master always plays at full on the speakers and MIX sets how much cue is blended on top (full cue at left). In two outputs, MIX feeds headphones only.'
	];
	const levelBullets = [
		'Headphone GAIN (Mixxx Head Gain). Scales the CUE path: the phones in two outputs, the cue ear in SPLIT, and the cue blend in MAIN.',
		'It does not change the room MASTER volume. Default is 1 (full). Turn down if the phones are hot.'
	];
	const splitBullets = [
		'Mono master on LEFT, mono cue on RIGHT of the same output.',
		'Needs a DJ splitter cable. A Y cable will not separate the legs.'
	];
	const ioBullets = ['Click for Speaker / Headphone CUE quick settings without interrupting audio.'];
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
	const modeBullets = [
		'practice: cue and master share the speakers.',
		'two outputs: MASTER/MAIN is the room, HEADPHONE CUE is headphones.',
		'split cable: mono master on LEFT, mono cue on RIGHT of one device.'
	];
	const masterPickBullets = [
		'Room mix. Pin this to speakers so OS-default headphones cannot steal the room. The MAIN speaker line cannot be interrupted by CUE unplug or reconnect.'
	];
	const cuePickBullets = [
		'Headphone CUE sink: wired or Bluetooth. Press CALIBRATE afterwards to time it against the room. Live Bluetooth pairing is CUEOUT-12, not this control.'
	];
	const inputPickBullets = [
		'Used to unlock output names and by CALIBRATE to time the chirps. Never pick a headphone/HFP mic.'
	];

	function openIo(): void {
		ioOpen = true;
		if (!ioAcquisitionRequested) {
			ioAcquisitionRequested = true;
			onacquire();
		}
	}

	function closeIo(): void {
		if (cueAlignModal.open) closeCueAlignModal();
		ioOpen = false;
	}

	function onWindowKeydown(event: KeyboardEvent): void {
		if (event.key === 'Escape' && ioOpen && !cueAlignModal.open) {
			event.preventDefault();
			closeIo();
		}
	}

	function updateDelay(event: Event): void {
		const input = event.currentTarget;
		if (!(input instanceof HTMLInputElement) || input.value === '') return;
		const value = Number(input.value);
		if (Number.isInteger(value) && value >= 0 && value <= 500) ondelay(value);
	}

	function scrollDelay(event: WheelEvent): void {
		const input = event.currentTarget;
		if (!(input instanceof HTMLInputElement) || document.activeElement !== input) return;
		event.preventDefault();
		const step = event.deltaY < 0 ? 1 : -1;
		ondelay(Math.max(0, Math.min(500, headphoneState.head_delay_ms + step)));
	}
</script>

<svelte:window onkeydown={onWindowKeydown} />

<div class="hp" data-performance-control="headphones">
	<ControlExplainer title="MIX" bullets={mixBullets} showDelayMs={60}>
		<span class="hp-control-icon" aria-hidden="true">🎧</span>
		<Knob
			knobId={knobId('hp', 'hp-mix')}
			label="MIX"
			accessibleLabel="Headphone CUE to MASTER mix"
			value={headphoneState.mix}
			onchange={onmix}
			onsingleclick={handleMixSingleClick}
			resetValue={0}
			accentColor={headphoneMixAccent(headphoneState.mix)}
		/>
	</ControlExplainer>
	<ControlExplainer title="VOL" bullets={levelBullets} showDelayMs={60}>
		<span class="hp-control-icon" aria-hidden="true">🎧</span>
		<Knob
			knobId={knobId('hp', 'hp-level')}
			label="VOL"
			accessibleLabel="Headphone cue gain"
			value={headphoneState.level}
			onchange={onlevel}
		/>
	</ControlExplainer>
	<ControlExplainer
		title="Audio I/O quick settings"
		bullets={ioBullets}
		showDelayMs={100}
		compact={true}
		disabled={ioOpen}
	>
		<button
			type="button"
			class="hp-btn hp-io-trigger"
			aria-label="SHOW AUDIO I/O"
			aria-expanded={ioOpen}
			onclick={openIo}>
			<span aria-hidden="true">🎧</span><span aria-hidden="true">ᛒ</span><span aria-hidden="true">🔊</span>
		</button
		>
	</ControlExplainer>
</div>

{#if ioOpen}
	<div class="hp-panel" role="dialog" aria-label="Audio I/O settings" aria-modal="false" tabindex="-1" data-audio-io-panel>
		<header class="hp-panel-header">
			<div>
				<strong>Audio I/O</strong>
				<small>Esc or X to dismiss</small>
			</div>
			<button type="button" class="hp-panel-close" aria-label="Close audio I/O settings" onclick={closeIo}>×</button>
		</header>
		<div class="hp-panel-body">
			<section class="hp-section" aria-label="Output routing">
				<h3>Routing <span data-output-mode={headphoneState.output_mode}>{modeLabel}</span></h3>
				<div class="hp-mode-choices">
					<button type="button" aria-label="Practice output mode" aria-pressed={headphoneState.output_mode === 'practice'} onclick={() => onmode('practice')}>MAIN / practice</button>
					<button type="button" aria-label="Two outputs output mode" aria-pressed={headphoneState.output_mode === 'two_outputs'} onclick={() => onmode('two_outputs')}>Two outputs</button>
					<button type="button" aria-label="Split cable output mode" aria-pressed={headphoneState.output_mode === 'split_cable'} title="Mono master left, mono cue right; use a DJ splitter cable" onclick={() => onmode('split_cable')}>SPLIT cable</button>
				</div>
				<p class="hp-context">{modeBullets[headphoneState.output_mode === 'practice' ? 0 : headphoneState.output_mode === 'two_outputs' ? 1 : 2]}</p>
				{#if headphoneState.output_mode === 'split_cable'}
					<p class="hp-warn" role="status" title="Split cable wiring">{splitBullets[0]} {splitBullets[1]}</p>
				{/if}
			</section>
			<section class="hp-section" aria-label="Future routing options">
				<div class="hp-future"><button type="button" disabled>BOOTH / MONITOR</button><span>Coming soon</span></div>
				<div class="hp-future"><button type="button" disabled>Advanced channel assignment</button><span>Coming soon</span></div>
			</section>
			<section class="hp-section" aria-label="Audio devices">
				<div class="hp-section-heading"><h3>Devices</h3><button type="button" aria-label="Rescan available headphone output devices" title="Rescan audio devices" onclick={onrefresh}>Rescan ↻</button></div>
				{#if masterLabel !== null || selectedLabel !== null}
					<p class="hp-context">{#if masterLabel !== null}MASTER: {masterLabel}. {/if}{#if selectedLabel !== null}CUE: {selectedLabel}.{/if}</p>
				{/if}
					{@render outputMenu()}
					<p class="hp-context">Signal lights measure app bus or mic input. They do not prove a physical speaker emitted sound.</p>
				{#if headphoneState.error !== null}
					<p class="hp-error" role="alert">{headphoneState.error}</p>
					<button type="button" class="hp-retry" onclick={onacquire}>Retry device access</button>
				{/if}
			</section>
			{#if headphoneState.output_mode === 'two_outputs'}
				<section class="hp-section" aria-label="Cue alignment">
					<h3>Cue alignment</h3>
					<p class="hp-context">Headphones can lead or lag MASTER, especially over Bluetooth. Delay the early path to align them.</p>
					<div class="hp-delay-visual" aria-hidden="true"><span>MASTER ━━━━━▶</span><span>CUE ━━━━━▶</span></div>
					<ControlExplainer title="HEAD DELAY" bullets={warningText === null ? delayBullets : [...delayBullets, warningText]} showDelayMs={100}>
						<label class="hp-delay"><span>HEAD DELAY</span><input type="number" min="0" max="500" step="1" value={headphoneState.head_delay_ms} aria-label="head delay milliseconds" data-performance-control="head-delay" oninput={updateDelay} onwheel={scrollDelay} /><span>ms</span></label>
					</ControlExplainer>
					<p class="hp-context">Click the value, then use ↑/↓ or two-finger scroll. Hover HEAD DELAY for timing guidance.</p>
					{#if headphoneState.master_delay_ms > 0}<p class="hp-room" data-performance-control="room-delay">ROOM +{headphoneState.master_delay_ms} ms. {roomBullets[0]}</p>{/if}
				</section>
			{/if}
			<section class="hp-section hp-calibration-section" aria-label="Calibration">
				<button type="button" class="hp-calibrate-button" aria-label="CALIBRATE CUE ALIGNMENT" data-performance-control="cue-calibrate" disabled={!calibrateEnabled} onclick={oncalibrate}>CALIBRATE</button>
				<p class="hp-context">{calibrateBullets[0]}</p>
				{#if cueAlignModal.open}
					<CueAlignModal headphones={headphoneState} onmode={onAlignmentMode} embedded={true} />
				{/if}
			</section>
		</div>
	</div>
{/if}

{#snippet outputMenu()}
	<div class="hp-menu">
		<ControlExplainer title="MASTER / MAIN" bullets={masterPickBullets} showDelayMs={40} placement="right">
			<label class="hp-pick">
				<span>MASTER / MAIN {@render signalIndicator('master')}</span>
				<select
					aria-label="master output device"
					value={headphoneState.selected_master_output_device_id ?? ''}
					disabled={!headphoneState.supported}
					onchange={(event) => onmaster(event.currentTarget.value)}
				>
					<option value="" disabled>choose master</option>
					{#each headphoneState.outputs as output (output.id)}
						<option value={output.id}>{output.label || output.id}</option>
					{/each}
				</select>
			</label>
		</ControlExplainer>
		{#if !headphoneState.supported}<p class="hp-context">Device selection needs supported browser or shell audio APIs and microphone permission.</p>{/if}
		<ControlExplainer title="HEADPHONE CUE" bullets={cuePickBullets} showDelayMs={40} placement="right">
			<label class="hp-pick">
				<span>HEADPHONE CUE {@render signalIndicator('cue')}</span>
				<select
					aria-label="headphone output device"
					value={headphoneState.selected_output_device_id ?? ''}
					disabled={!headphoneState.supported}
					onchange={(event) => onselect(event.currentTarget.value)}
				>
					<option value="" disabled>HP out</option>
					{#each headphoneState.outputs as output (output.id)}
						<option value={output.id}>{output.label || output.id}</option>
					{/each}
				</select>
			</label>
		</ControlExplainer>
		{#if selectedLabel !== null && monitorLabelIsBluetooth(selectedLabel)}<p class="hp-context">Bluetooth CUE can drift against MASTER. Use CALIBRATE for a measured starting offset.</p>{/if}
		{#if livenessAlert !== null}<p class="hp-liveness-alert" role="alert" data-headphone-liveness-alert>{livenessAlert}</p>{/if}
		<ControlExplainer title="AUDIO IN" bullets={inputPickBullets} showDelayMs={40} placement="right">
			<label class="hp-pick">
				<span>AUDIO IN {@render signalIndicator('input')}</span>
				<select
					aria-label="audio input device"
					value={headphoneState.selected_input_device_id ?? ''}
					onchange={(event) => oninput(event.currentTarget.value)}
				>
					<option value="" disabled>choose input</option>
					{#each headphoneState.inputs as input (input.id)}
						<option value={input.id}>{input.label || input.id}</option>
					{/each}
				</select>
			</label>
		</ControlExplainer>
	</div>
{/snippet}

{#snippet signalIndicator(bus: 'master' | 'cue' | 'input')}
	{@const reading = signals?.[bus]}
	<span
		class="hp-signal"
		class:lit={reading?.state === 'measured' && reading.rms !== null && reading.rms > 0.002}
		data-signal-bus={bus}
		data-signal-state={reading?.state ?? 'unavailable'}
		aria-label={`${bus} ${reading?.state === 'measured' && reading.rms !== null ? `${reading.source === 'captured_input' ? 'captured input' : 'application bus'} signal ${reading.rms.toFixed(2)}` : reading?.state ?? 'unavailable'}; physical output not proven`}
		title={reading?.state === 'measured' && reading.rms !== null ? `Measured ${reading.source === 'captured_input' ? 'mic input' : 'application bus'} RMS ${reading.rms.toFixed(3)}. Physical output not proven.` : `Signal ${reading?.state ?? 'unavailable'}. No measured output proof.`}
	></span>
{/snippet}

<style>
	.hp {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 4px;
		min-width: 0;
	}
	.hp-control-icon {
		font-size: 9px;
		line-height: 1;
		margin-right: 2px;
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
	.hp-btn:hover {
		color: var(--rb-text, #c8cdd2);
		border-color: var(--rb-accent, #2f6fd6);
	}
	.hp-io-trigger {
		max-width: none;
		font-size: 10px;
		display: inline-flex;
		gap: 2px;
		align-items: center;
	}
	.hp-panel {
		position: fixed;
		z-index: 140;
		top: max(12px, var(--rb-topbar-h, 50px));
		right: 12px;
		width: min(450px, calc(100vw - 24px));
		max-height: calc(100vh - 24px);
		display: flex;
		flex-direction: column;
		background: var(--rb-panel, #14171b);
		color: var(--rb-text, #c8cdd2);
		border: 1px solid var(--rb-border, #333);
		border-radius: 8px;
		box-shadow: 0 14px 36px rgba(0, 0, 0, 0.55);
		font-size: 11px;
	}
	.hp-panel-header, .hp-section-heading {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
	}
	.hp-panel-header {
		padding: 10px 14px;
		border-bottom: 1px solid var(--rb-border, #333);
	}
	.hp-panel-header strong { display: block; font-size: 14px; }
	.hp-panel-header small { display: block; color: var(--rb-text-dim, #838990); font-size: 9px; }
	.hp-panel-close {
		background: transparent;
		border: 0;
		color: var(--rb-text, #c8cdd2);
		font-size: 20px;
		cursor: pointer;
	}
	.hp-panel-body { overflow: auto; padding: 10px 14px 14px; }
	.hp-section { padding: 8px 0; border-bottom: 1px solid var(--rb-border, #333); }
	.hp-section:last-child { border-bottom: 0; }
	.hp-section h3 { margin: 0 0 6px; font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }
	.hp-section h3 span { color: var(--rb-text-dim, #838990); text-transform: none; font-weight: 400; }
	.hp-section-heading h3 { margin: 0; }
	.hp-section button, .hp-section select, .hp-section input { font: inherit; }
	.hp-mode-choices { display: flex; flex-wrap: wrap; gap: 5px; }
	.hp-mode-choices button, .hp-section-heading button, .hp-calibrate-button {
		background: var(--rb-panel-raised, #1a1e25);
		border: 1px solid var(--rb-border, #333);
		border-radius: 3px;
		color: var(--rb-text, #c8cdd2);
		padding: 4px 7px;
		cursor: pointer;
	}
	.hp-mode-choices button[aria-pressed='true'] { border-color: var(--rb-accent, #4fb3ff); color: var(--rb-accent, #4fb3ff); }
	.hp-context { color: var(--rb-text-dim, #838990); font-size: 10px; line-height: 1.35; margin: 5px 0; }
	.hp-future { display: flex; justify-content: space-between; align-items: center; color: var(--rb-text-dim, #838990); margin: 3px 0; }
	.hp-future button { background: transparent; border: 0; color: inherit; padding: 3px 0; }
	.hp-future span { font-size: 9px; }
	.hp-future button:disabled, .hp-calibrate-button:disabled { opacity: .5; cursor: not-allowed; }
	.hp-delay-visual { display: flex; flex-direction: column; gap: 2px; color: var(--rb-accent, #4fb3ff); font-size: 9px; letter-spacing: .05em; margin: 5px 0; }
	.hp-calibration-section { display: block; }
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
		min-width: 0;
	}
	.hp-pick {
		display: flex;
		flex-direction: column;
		gap: 2px;
		font-size: 10px;
		letter-spacing: 0.04em;
		color: var(--rb-text-dim, #838990);
	}
	.hp-pick select {
		max-width: 100%;
		font-size: 11px;
	}
	.hp-signal { display: inline-block; width: 6px; height: 6px; border-radius: 50%; margin-left: 3px; background: var(--rb-text-dim, #838990); vertical-align: middle; opacity: .45; }
	.hp-signal.lit { background: var(--rb-green, #35c04f); opacity: 1; box-shadow: 0 0 4px var(--rb-green, #35c04f); }
	.hp-error {
		color: var(--rb-danger, #ff6b6b);
		font-size: 10px;
	}
	.hp-delay {
		display: inline-flex;
		align-items: center;
		gap: 2px;
		font-size: 10px;
		color: var(--rb-text-dim, #838990);
	}
	.hp-delay input {
		font: inherit;
		font-size: 11px;
		width: 54px;
		padding: 0 2px;
		background: var(--rb-panel-raised, #1a1e25);
		border: 1px solid var(--rb-border, #23282f);
		color: var(--rb-text-dim, #838990);
	}
	.hp-btn:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}
	.hp-room {
		font-size: 10px;
		letter-spacing: 0.04em;
		color: var(--rb-accent, #4fb3ff);
		white-space: nowrap;
	}
	.hp-warn {
		color: var(--rb-warn, #e6a23c);
		font-size: 10px;
	}
	.hp-liveness-alert {
		color: var(--rb-danger, #ff6b6b);
		font-size: 10px;
		font-weight: 600;
		line-height: 1.2;
	}
</style>
