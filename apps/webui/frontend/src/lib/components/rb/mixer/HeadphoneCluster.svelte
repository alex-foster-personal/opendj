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
	import { headphoneLivenessAlertForState, headphoneMixAccent, MIC_DECLINED_NOTICE, monitorLabelIsBluetooth, twoOutputsWarning } from '$lib/player/headphones';
	import { calibrateButtonEnabled } from '$lib/player/cue-align-policy';
	import { levelBullets, delayBullets, calibrateBullets, roomBullets, rescanBullets, modeBullets, inputPickBullets } from './headphone-io-copy';
	import { closeCueAlignModal, cueAlignModal } from '$lib/rb/cue-align-session.svelte';
	import ControlExplainer from '../deck/ControlExplainer.svelte';
	import CueAlignModal from './CueAlignModal.svelte';
	import { CLOSE_PATH, PLAY_TRIANGLE_PATH, RESCAN_ARROW_PATH, RESCAN_PATH } from '$lib/ui/icon-glyphs';
	import { closeIoView, openIoView, ioSurface } from '$lib/rb/io-surface.svelte';
	import { audioOutputStatus, djioFallbackTitle } from '$lib/rb/audio-output-status.svelte';
	import { toggleMidiPanel, midiUi, midiEnabledPersisted } from '$lib/components/rb/midi/midi-ui-state.svelte';
	import { midiLabelGlyph, midiLabelStatus, midiLabelTitle } from '$lib/components/rb/midi/midi-format';
	import { midiState } from '$lib/rb/midi/midi-state.svelte';
	import { ioHoverSummary, ioShouldAlert, outputsUnset } from '$lib/rb/io-outputs-button';
	import { ioOutputsSession, noteSetOutputsClicked } from '$lib/rb/io-outputs-session.svelte';
	import Knob from './Knob.svelte';
	import MidiStatusGlyph from './MidiStatusGlyph.svelte';
	import type { HeadphoneAlignmentMode, HeadphoneOutputMode, HeadphoneState } from '$lib/rb/mixer-types';

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
	const livenessAlert = $derived(headphoneLivenessAlertForState(headphoneState));
	// IOPIN-14: the panel says whether the lists could be read before it shows them.
	const access = $derived(headphoneState.device_access);
	const accessActionLabel = $derived(
		access.action === 'grant' ? 'Grant access to list devices' : 'Retry device check'
	);
	// The denied notice already says this; one statement of it is enough.
	const showError = $derived(
		headphoneState.error !== null && !(access.status === 'permission_denied' && headphoneState.error === MIC_DECLINED_NOTICE)
	);
	const mixBullets = [
		'Turn MIX left: more channel CUE in the blend. Turn right: more MASTER.',
		'Single-click the knob to step toward the other extreme.',
		'MAIN (practice): master stays full on speakers; MIX blends cue on top. Two outputs: MIX is headphones only.',
		'Left is full CUE (orange). Right is full MASTER (blue). Default is full CUE.'
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
	// Pin 894af5672c3b (main #3990), merged onto the Preview's persistent I/O
	// panel: the I/O hover is compact (one sentence, the devices in use, the
	// call to action) and dismisses instantly; the per-control detail lives
	// on each control's own explainer inside the panel.
	const ioSummary = $derived(ioHoverSummary(headphoneState));
	const ioBullets = $derived([ioSummary.sentence, ...ioSummary.devices, ioSummary.cta]);
	// SET OUTPUTS pulses and glows red while no output is chosen, until the
	// first click this session (sessionStorage; a blocked store falls back to
	// the module's in-memory flag).
	const ioAlert = $derived(ioShouldAlert(outputsUnset(headphoneState), ioOutputsSession.clicked));

	// The click only opens the panel (Preview d16ef5f5: opening stays side-effect
	// free); device access is the panel's explicit "Choose output" action.
	function handleSetOutputs(): void {
		noteSetOutputsClicked();
		openIo();
	}
	const masterPickBullets = [
		'Room mix. Pin this to speakers so OS-default headphones cannot steal the room. The MAIN speaker line cannot be interrupted by CUE unplug or reconnect.'
	];
	const cuePickBullets = [
		'Headphone CUE sink: wired or Bluetooth. Press CALIBRATE afterwards to time it against the room. Live Bluetooth pairing is CUEOUT-12, not this control.'
	];

	function stepHeadDelay(delta: number): void {
		ondelay(Math.min(500, Math.max(0, headphoneState.head_delay_ms + delta)));
	}

	function openIo(): void {
		openIoView();
	}

	function closeIo(): void {
		if (cueAlignModal.open) closeCueAlignModal();
		closeIoView();
	}

	function onWindowKeydown(event: KeyboardEvent): void {
		if (event.key === 'Escape' && ioSurface.open && !cueAlignModal.open) {
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

	/** The value is a text field (MIXUX-08), so the arrow keys step it here. */
	function keyDelay(event: KeyboardEvent): void {
		if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return;
		event.preventDefault();
		stepHeadDelay(event.key === 'ArrowUp' ? 1 : -1);
	}

	function scrollDelay(event: WheelEvent): void {
		const input = event.currentTarget;
		if (!(input instanceof HTMLInputElement) || document.activeElement !== input) return;
		event.preventDefault();
		const step = event.deltaY < 0 ? 1 : -1;
		ondelay(Math.max(0, Math.min(500, headphoneState.head_delay_ms + step)));
	}

	// MIDI status moved here with MIDI connect (CHROME-07): the entry keeps the
	// gray / amber / green / red reading the top-bar MIDI label used to carry.
	// Logic lives in midi-format.ts (pure, unit-tested); this is the plumbing.
	// Turning MIDI off keeps the browser's grant and empties the device list, so
	// the persisted choice tells an explicit off (gray) from a lost device (red).
	const midiMappedCount = $derived(midiState.devices.filter((d) => d.mapVendor !== null).length);
	const midiOn = $derived(midiEnabledPersisted());
	const midiStatus = $derived(midiLabelStatus(midiState.permission, midiUi.requestPending, midiMappedCount > 0, midiOn));
	const midiGlyph = $derived(midiLabelGlyph(midiStatus));
	const midiTitle = $derived(midiLabelTitle(midiState.permission, midiUi.requestPending, midiMappedCount, midiState.devices.length, midiOn));

	// Both MIDI entries open the drawer and unpin the I/O view (its z-index 80 would cover the drawer's 41).
	function openMidiDrawer(): void {
		if (!midiUi.panelOpen) toggleMidiPanel();
		closeIoView();
	}
</script>

<svelte:window onkeydown={onWindowKeydown} />

<div class="hp" data-performance-control="headphones">
	<ControlExplainer title="MIX" bullets={mixBullets} demo="headphone-mix" showDelayMs={60}>
		<svg class="hp-control-icon" viewBox="0 0 12 12" width="10" height="10" aria-hidden="true"><path d="M2 8 V6 a4 4 0 0 1 8 0 V8" fill="none" stroke="currentColor" stroke-width="1.2" /><rect x="1.2" y="7" width="2.3" height="3.6" rx="0.8" fill="currentColor" /><rect x="8.5" y="7" width="2.3" height="3.6" rx="0.8" fill="currentColor" /></svg>
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
		<svg class="hp-control-icon" viewBox="0 0 12 12" width="10" height="10" aria-hidden="true"><path d="M2 8 V6 a4 4 0 0 1 8 0 V8" fill="none" stroke="currentColor" stroke-width="1.2" /><rect x="1.2" y="7" width="2.3" height="3.6" rx="0.8" fill="currentColor" /><rect x="8.5" y="7" width="2.3" height="3.6" rx="0.8" fill="currentColor" /></svg>
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
		disabled={ioSurface.open}
		dismiss="instant"
	>
		<button
			type="button"
			class="hp-btn hp-io-trigger hp-btn-io"
			class:io-alert={ioAlert}
			data-io-alert={ioAlert ? 'unset' : undefined}
			aria-label="SHOW AUDIO I/O"
			aria-expanded={ioSurface.open}
			onclick={handleSetOutputs}><span>SET</span><span>OUTPUTS</span></button
		>
	</ControlExplainer>
	<ControlExplainer
		title="MIDI"
		bullets={[midiTitle, 'Open the MIDI panel to connect controllers and view the learn log.']}
		showDelayMs={60}
	>
		<button
			type="button"
			class="hp-btn midi-btn st-{midiStatus}"
			aria-label="Open MIDI panel"
			aria-expanded={midiUi.panelOpen}
			onclick={openMidiDrawer}
			>MIDI<MidiStatusGlyph glyph={midiGlyph} /></button
		>
	</ControlExplainer>
</div>

{#if ioSurface.open}
	<div class="hp-panel" role="dialog" aria-label="Audio I/O settings" aria-modal="false" tabindex="-1" data-audio-io-panel>
		<header class="hp-panel-header">
			<div>
				<strong>Audio I/O</strong>
				<small>Esc or X to dismiss</small>
			</div>
			<button type="button" class="hp-panel-close" aria-label="Close audio I/O settings" onclick={closeIo}><svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><path d={CLOSE_PATH} fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" /></svg></button>
		</header>
		<div class="hp-panel-body">
			<section class="hp-section" aria-label="Output routing">
				<h3>Routing <ControlExplainer title="Output mode" bullets={modeBullets} demo="headphone-mode" showDelayMs={60}><span class="hp-mode" data-output-mode={headphoneState.output_mode}>{modeLabel}</span></ControlExplainer></h3>
				<div class="hp-mode-choices">
					<ControlExplainer title="MAIN" bullets={mainBullets} demo="headphone-practice" showDelayMs={60}><button type="button" aria-label="Practice output mode" aria-pressed={headphoneState.output_mode === 'practice'} onclick={() => onmode('practice')}>MAIN / practice</button></ControlExplainer>
					<button type="button" aria-label="Two outputs output mode" aria-pressed={headphoneState.output_mode === 'two_outputs'} onclick={() => onmode('two_outputs')}>Two outputs</button>
					<ControlExplainer title="SPLIT" bullets={splitBullets} demo="headphone-split" showDelayMs={60}><button type="button" aria-label="Split cable output mode" aria-pressed={headphoneState.output_mode === 'split_cable'} title="Mono master left, mono cue right; use a DJ splitter cable" onclick={() => onmode('split_cable')}>SPLIT cable</button></ControlExplainer>
				</div>
				<p class="hp-context">{modeBullets[headphoneState.output_mode === 'practice' ? 0 : headphoneState.output_mode === 'two_outputs' ? 1 : 2]}</p>
				{#if audioOutputStatus.fallback !== null}<p class="hp-warn" role="status" data-djio-fallback-notice title={djioFallbackTitle(audioOutputStatus.fallback)}>{audioOutputStatus.fallback.message}</p>{/if}
				{#if headphoneState.output_mode === 'split_cable'}
					<ControlExplainer title="SPLIT warning" bullets={splitBullets} showDelayMs={60}><p class="hp-warn" role="status" title="Split cable wiring">{splitBullets[0]} {splitBullets[1]}</p></ControlExplainer>
				{/if}
			</section>
			<section class="hp-section" aria-label="Future routing options">
				<div class="hp-future"><button type="button" disabled>BOOTH / MONITOR</button><span>Coming soon</span></div>
				<div class="hp-future"><button type="button" disabled>Advanced channel assignment</button><span>Coming soon</span></div>
			</section>
			<section class="hp-section" aria-label="Audio devices">
				<div class="hp-section-heading"><h3>Devices</h3><ControlExplainer title="Rescan" bullets={rescanBullets} showDelayMs={100}><button type="button" aria-label="Rescan available headphone output devices" onclick={onrefresh}><svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><path d={RESCAN_PATH} fill="none" stroke="currentColor" stroke-width="1.2" /><path d={RESCAN_ARROW_PATH} fill="currentColor" /></svg> Rescan</button></ControlExplainer></div>
			<button type="button" class="hp-acquire" onclick={onacquire}>Choose output / allow device access</button>
			<p class="hp-context">Device access can open an output chooser or microphone permission prompt. It may change the CUE route; use it deliberately.</p>
				<div class="hp-access" data-io-device-access={access.status}>
					{#if access.message !== null}
						<p class="hp-warn" role="status" data-io-device-access-message title={access.detail ?? access.message}>{access.message}{#if access.detail !== null} <span class="hp-access-detail">({access.detail})</span>{/if}</p>
					{/if}
					{#if access.action !== 'none'}
						<button type="button" class="hp-retry" data-io-device-access-action={access.action} onclick={access.action === 'grant' ? onacquire : onrefresh}>{accessActionLabel}</button>
					{/if}
					{#each access.notices as notice (notice)}
						<p class="hp-context" role="status" data-io-device-notice>{notice}</p>
					{/each}
				</div>
				{#if masterLabel !== null || selectedLabel !== null}
					<p class="hp-context">{#if masterLabel !== null}MASTER: {masterLabel}. {/if}{#if selectedLabel !== null}CUE: {selectedLabel}.{/if}</p>
				{/if}
					{@render outputMenu()}
					<p class="hp-context">Signal lights measure app bus or mic input. They do not prove a physical speaker emitted sound.</p>
				{#if showError}
					<p class="hp-error" role="alert">{headphoneState.error}</p>
					<button type="button" class="hp-retry" onclick={onacquire}>Retry device access</button>
				{/if}
			</section>
			{#if headphoneState.output_mode === 'two_outputs'}
				<section class="hp-section" aria-label="Cue alignment">
					<h3>Cue alignment</h3>
					<p class="hp-context">Headphones can lead or lag MASTER, especially over Bluetooth. Delay the early path to align them.</p>
					<div class="hp-delay-visual" aria-hidden="true"><span>MASTER ━━━━━<svg viewBox="0 0 16 16" width="7" height="7"><path d={PLAY_TRIANGLE_PATH} fill="currentColor" /></svg></span><span>CUE ━━━━━<svg viewBox="0 0 16 16" width="7" height="7"><path d={PLAY_TRIANGLE_PATH} fill="currentColor" /></svg></span></div>
					<ControlExplainer title="HEAD DELAY" bullets={warningText === null ? delayBullets : [...delayBullets, warningText]} showDelayMs={100}>
						<label class="hp-delay"><span>HEAD DELAY</span><span class="hp-delay-stepper" role="group" aria-label="head delay stepper"><button type="button" class="hp-delay-step" aria-label="increase head delay" onclick={() => stepHeadDelay(1)}><svg viewBox="0 0 10 6" width="10" height="6" aria-hidden="true"><path d="M1 5 L5 1 L9 5" fill="none" stroke="currentColor" stroke-width="1.4" /></svg></button><button type="button" class="hp-delay-step" aria-label="decrease head delay" onclick={() => stepHeadDelay(-1)}><svg viewBox="0 0 10 6" width="10" height="6" aria-hidden="true"><path d="M1 1 L5 5 L9 1" fill="none" stroke="currentColor" stroke-width="1.4" /></svg></button></span><input type="text" inputmode="numeric" pattern="[0-9]*" value={headphoneState.head_delay_ms} aria-label="head delay milliseconds" data-performance-control="head-delay" oninput={updateDelay} onkeydown={keyDelay} onwheel={scrollDelay} /><span>ms</span></label>
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
					disabled={!headphoneState.supported || !access.output_pinning}
					onchange={(event) => onmaster(event.currentTarget.value)}
				>
					<option value="" disabled>choose master</option>
					{#each headphoneState.outputs as output (output.id)}
						<option value={output.id}>{output.label || output.id}</option>
					{/each}
				</select>
			</label>
		</ControlExplainer>
		<ControlExplainer title="HEADPHONE CUE" bullets={cuePickBullets} showDelayMs={40} placement="right">
			<label class="hp-pick">
				<span>HEADPHONE CUE {@render signalIndicator('cue')}</span>
				<select
					aria-label="headphone output device"
					value={headphoneState.selected_output_device_id ?? ''}
					disabled={!headphoneState.supported || !access.output_pinning}
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
		<!-- CHROME-07: the tray's "Open audio I/O and MIDI" lands here, so MIDI connect lives inside this view. -->
		<ControlExplainer title="MIDI" showDelayMs={40} placement="right"
			bullets={[midiTitle, 'Open the MIDI panel to connect controllers and view the learn log.']}>
			<button
				type="button"
				class="hp-btn midi-btn io-midi st-{midiStatus}"
				aria-label="Open MIDI panel from audio I/O"
				aria-expanded={midiUi.panelOpen}
				onclick={openMidiDrawer}>MIDI<MidiStatusGlyph glyph={midiGlyph} /></button
			>
		</ControlExplainer>
	</div>
{/snippet}

{#snippet signalIndicator(bus: 'master' | 'cue' | 'input')}
	{@const reading = headphoneState.signals[bus]}
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
	@import './HeadphoneCluster.io-panel.css';

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
	.midi-btn.st-grey {
		opacity: 0.6;
	}
	.midi-btn.st-green {
		color: var(--rb-green);
	}
	.midi-btn.st-red {
		color: var(--rb-red);
	}
	.midi-btn.st-amber {
		color: var(--rb-orange);
		animation: midi-pulse 1s ease-in-out infinite;
	}
	@keyframes midi-pulse {
		0%,
		100% {
			opacity: 1;
		}
		50% {
			opacity: 0.35;
		}
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
	.hp-panel-header strong { display: block; font-size: 14px; }
	.hp-panel-header small { display: block; color: var(--rb-text-dim, #838990); font-size: 9px; }
	.hp-section h3 { margin: 0 0 6px; font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }
	.hp-section h3 span { color: var(--rb-text-dim, #838990); text-transform: none; font-weight: 400; }
	.hp-section-heading h3 { margin: 0; }
	.hp-section button, .hp-section select, .hp-section input { font: inherit; }
	.hp-mode-choices button, .hp-section-heading button, .hp-calibrate-button, .hp-acquire {
		background: var(--rb-panel-raised, #1a1e25);
		border: 1px solid var(--rb-border, #333);
		border-radius: 3px;
		color: var(--rb-text, #c8cdd2);
		padding: 4px 7px;
		cursor: pointer;
	}
	.hp-mode-choices button[aria-pressed='true'] { border-color: var(--rb-accent, #4fb3ff); color: var(--rb-accent, #4fb3ff); }
	.hp-future button { background: transparent; border: 0; color: inherit; padding: 3px 0; }
	.hp-future span { font-size: 9px; }
	.hp-future button:disabled, .hp-calibrate-button:disabled { opacity: .5; cursor: not-allowed; }
	select {
		font: inherit;
		font-size: 7px;
		max-width: 72px;
		padding: 0 2px;
		background: var(--rb-panel-raised, #1a1e25);
		border: 1px solid var(--rb-border, #23282f);
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
	.hp-delay input {
		font: inherit;
		font-size: 11px;
		width: 54px;
		padding: 0 2px;
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
