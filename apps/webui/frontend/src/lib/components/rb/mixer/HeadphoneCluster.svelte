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
	import { headphoneLivenessAlertForState, headphoneMixAccent, inputLooksLikeHandsfree, MIC_DECLINED_NOTICE, monitorLabelIsBluetooth, nativeCueSinkAvailable, twoOutputsWarning } from '$lib/player/headphones';
	import { ioMeterFill, ioTestDeviceId, ioTestEnvelope, ioTestRefusal, passiveInputMeterBlockedReason, type IoTestBus } from '$lib/player/io-device-test';
	import { calibrateButtonEnabled } from '$lib/player/cue-align-policy';
	import { recordPerfEvent } from '$lib/rb/perf-event-log';
	import { plannedTitle } from '$lib/rb/planned-explainers';
	import { remoteSpeakerBuildFlag } from '$lib/player/remote-speaker-flag';
	import { pickThenMirror, type DevicePick } from './device-select-mirror';
	import { outputOptionLabel, wirelessMainNote } from '$lib/player/wireless-output';
	import { levelBullets, delayBullets, calibrateBullets, roomBullets, rescanBullets, modeBullets, inputPickBullets } from './headphone-io-copy';
	import { closeCueAlignModal, cueAlignModal } from '$lib/rb/cue-align-session.svelte';
	import ControlExplainer from '../deck/ControlExplainer.svelte';
	import CueAlignModal from './CueAlignModal.svelte';
	import SpeakerLinkSection from './SpeakerLinkSection.svelte';
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

	/** Whether this engine knows the `:open` pseudo-class (older engines do not). */
	const OPEN_PSEUDO_SUPPORTED = typeof CSS !== 'undefined' && CSS.supports('selector(:open)');

	interface Props {
		state: HeadphoneState;
		onmix: (value: number) => void;
		onlevel: (value: number) => void;
		ondelay: (value: number) => void;
		onrefresh: () => void;
		onacquire: () => void;
		onselect: DevicePick;
		onmaster: DevicePick;
		oninput: DevicePick;
		onmode: (mode: HeadphoneOutputMode) => void;
		/** CUEOUT-14: open the mic calibration modal. */
		oncalibrate: () => void;
		onAlignmentMode: (mode: HeadphoneAlignmentMode) => void;
		/** Runs a Test under the dispatcher's ownership (`runAudioIoTest`): it
		 * rejects with no route session or while a preset owns the lifecycle. */
		testrun: <T>(work: (stillCurrent: () => boolean, signal: AbortSignal) => Promise<T>) => Promise<T>;
	}

	let { state: headphoneState, onmix, onlevel, ondelay, onrefresh, onacquire, onselect, onmaster, oninput, onmode, oncalibrate, onAlignmentMode, testrun }: Props =
		$props();

	/** I18: the remote speaker build flag (on in dev, off in built apps). */
	const remoteSpeakerOn = remoteSpeakerBuildFlag() === 'on';
	// Started when the cluster mounts, not when the panel opens, so the section is
	// usually loaded before the panel shows and does not grow it under the pointer.
	const remoteSpeakerSection = remoteSpeakerOn ? import('./RemoteSpeakerSection.svelte') : null;

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
	const wirelessMain = $derived(
		wirelessMainNote(headphoneState.outputs, headphoneState.selected_master_output_device_id)
	);
	const calibrateEnabled = $derived(
		calibrateButtonEnabled({
			output_mode: headphoneState.output_mode,
			selected_output_device_id: headphoneState.selected_output_device_id
		})
	);

	// Pin 3cfdec80ba0a: the preferred mic is not a choice. Calibration will not
	// start until the operator picks the mic that can hear the speakers.
	let calibrationMicId = $state('');
	/** Shown in AUDIO IN immediately, before the headphone command lands. */
	let audioInPendingId = $state<string | null>(null);
	const audioInValue = $derived(audioInPendingId ?? headphoneState.selected_input_device_id ?? '');

	async function chooseCalibrationMic(deviceId: string): Promise<void> {
		if (deviceId.trim() === '') return;
		calibrationMicId = deviceId;
		audioInPendingId = deviceId;
		try {
			await oninput(deviceId);
		} finally {
			// IOPIN-16: once the pick settles, AUDIO IN shows what the router
			// holds, so a refused pick does not stay on screen as pending.
			if (audioInPendingId === deviceId) audioInPendingId = null;
		}
	}

	function selectValue(event: Event): string {
		const select = event.currentTarget;
		return select instanceof HTMLSelectElement ? select.value : '';
	}

	$effect(() => {
		if (calibrationMicId !== '' && !headphoneState.inputs.some((input) => input.id === calibrationMicId)) {
			calibrationMicId = '';
		}
		if (audioInPendingId !== null && headphoneState.selected_input_device_id === audioInPendingId) {
			audioInPendingId = null;
		}
	});

	// IOPIN-17: the mode is named by how many outputs it uses. "MAIN" names
	// only the room output (the MASTER / MAIN pick), never a mode.
	const modeLabel = $derived(
		headphoneState.output_mode === 'practice'
			? 'one output'
			: headphoneState.output_mode === 'two_outputs'
				? 'two outputs'
				: headphoneState.split_reason === 'same_device'
					? 'split L/R (shared output)'
					: 'split L/R'
	);
	// IOPIN-16: in a same-device split there is no separate CUE sink; the CUE
	// pick says where the cue went instead of a bare placeholder.
	const cuePlaceholder = $derived(
		headphoneState.split_reason === 'same_device' && headphoneState.output_mode === 'split_cable'
			? `right ear of MAIN (split L/R)`
			: 'choose headphones'
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
		'One output: master stays full on speakers; MIX blends cue on top. Two outputs: MIX is headphones only.',
		'Left is full CUE (orange). Right is full MASTER (blue). Default is full CUE.'
	];
	const mainBullets = [
		'1) Press ONE OUTPUT for a laptop or any single output (practice).',
		'2) Turn CUE on for each channel you want in the headphone blend.',
		'3) With no HEADPHONE CUE device picked, MIX left adds more cue into the speaker mix; master stays full.',
		'4) Picking HEADPHONE CUE in I/O switches to two outputs: room on MASTER/MAIN, cue on the phones.'
	];
	const splitBullets = [
		'1) Press SPLIT L/R for a DJ splitter cable: mono master on LEFT, mono cue on RIGHT.',
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
		'Room mix. Pin this to speakers so OS-default headphones cannot steal the room. The MAIN speaker line cannot be interrupted by CUE unplug or reconnect.',
		'Test plays a few beeps on this output. The thin meter is the level on this bus.'
	];
	const cuePickBullets = [
		'Headphone CUE sink: wired or Bluetooth. Press CALIBRATE afterwards to time it against the room. Live Bluetooth pairing is CUEOUT-12, not this control.',
		'Test plays a few beeps on this output. The thin meter is the level on this bus.'
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

	// Pin 5d0378165889: one outside click still drives the decks. The second hides I/O.
	let ioPanelEl: HTMLDivElement | undefined = $state();
	let ioOutsideClicks = 0;
	// Set by a press on a select inside the panel: its native menu paints
	// outside the panel and reports the next hit on the page root. Consumed by
	// that one root hit, or cleared once the select commits, loses focus, or
	// its menu is canceled (Escape, or a second press on the same select that
	// closes it), so a canceled menu never swallows a later genuine press.
	let ioSelectMenuOpen = false;
	let ioSelectMenuOwner: HTMLSelectElement | null = null;

	$effect(() => {
		if (!ioSurface.open) {
			ioOutsideClicks = 0;
			_clearIoSelectMenu();
		}
	});

	function onIoPanelSelectSettled(event: Event): void {
		if (event.target instanceof HTMLSelectElement) _clearIoSelectMenu();
	}

	function onIoPanelKeydown(event: KeyboardEvent): void {
		if (event.key === 'Escape' && event.target instanceof HTMLSelectElement) _clearIoSelectMenu();
	}

	/** Whether the armed select's native menu is still open. `:open` reports it
	 * directly, so a menu canceled with Escape (a key the menu consumes before
	 * the page sees it) reads closed; an engine without `:open` falls back to
	 * the armed flag. Support is a capability check, not a caught exception, so
	 * any other selector failure propagates (Sol P1 4290994508). */
	function _ioSelectMenuStillOpen(): boolean {
		if (!ioSelectMenuOpen || ioSelectMenuOwner === null) return false;
		if (!OPEN_PSEUDO_SUPPORTED) return true;
		return ioSelectMenuOwner.matches(':open');
	}

	function _clearIoSelectMenu(): void {
		ioSelectMenuOpen = false;
		ioSelectMenuOwner = null;
	}

	function pinPlacementArmed(): boolean {
		const armed = (window as unknown as Record<string, unknown>).__mdtPinPlacementArmed as
			| { get?: () => boolean }
			| undefined;
		return armed?.get?.() === true;
	}

	function onIoOutsidePointerDown(event: PointerEvent): void {
		if (!ioSurface.open || event.button !== 0) return;
		const target = event.target;
		if (!(target instanceof Node)) return;
		if (ioPanelEl?.contains(target)) {
			ioOutsideClicks = 0;
			// Any press on a select arms the one-hit skip. Whether a later root hit
			// really is that select's menu is decided then, from `:open`
			// (`_ioSelectMenuStillOpen`), so a press that closes the menu, or a
			// menu canceled with Escape, skips nothing (Codex P2 4286965506). An
			// engine without `:open` skips the next root hit: at worst one more
			// outside press is needed, never one fewer.
			const select = target instanceof Element ? target.closest('select') : null;
			if (select !== null) {
				ioSelectMenuOpen = true;
				ioSelectMenuOwner = select;
			} else {
				_clearIoSelectMenu();
			}
			return;
		}
		// The openers re-call openIoView on click, so they must not count as "outside".
		if (target instanceof Element && target.closest('.hp-io-trigger, .tray-midi, .fb-place-skip') !== null) return;
		if (pinPlacementArmed()) return;
		// A native select menu paints outside the panel and reports the hit on the
		// page root: skip exactly that one hit. Focus alone is not the signal (Sol
		// P1 4285298949): a select keeps focus during a later genuine root press.
		if ((target === document.documentElement || target === document.body) && _ioSelectMenuStillOpen()) {
			_clearIoSelectMenu();
			return;
		}
		_clearIoSelectMenu();
		ioOutsideClicks += 1;
		if (ioOutsideClicks >= 2) {
			ioOutsideClicks = 0;
			closeIo();
		}
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

	// Pin 17590d2ccb4f: each device row gets a beep and a thin vertical meter.
	// The input cannot play, so its beeps go out the master path and its meter
	// follows the microphone.
	let testBus = $state<IoTestBus | null>(null);
	let testStartedAt = $state(0);
	let nowMs = $state(0);
	let testNote = $state<string | null>(null);
	let inputLiveRms = $state(0);
	/** Why the passive live input meter could not open, shown under the pickers
	 * and in the meter's title rather than leaving it looking merely idle. */
	let inputMeterError = $state<string | null>(null);
	/** CUEOUT-8393: why the passive AUDIO IN meter is closed, shown, never hidden. */
	let inputMeterNote = $state<string | null>(null);
	// Bumped after an AUDIO IN Test opened the microphone, so the passive live
	// meter re-checks access (now granted) and stays live once the Test ends.
	let inputRecheck = $state(0);
	let testToken = 0;
	// Aborts the running Test's microphone open (a permission prompt left up)
	// when the panel closes, unmounts or another Test starts.
	let testAbort: AbortController | null = null;
	function abortTest(): void {
		testAbort?.abort(new Error('The Test was cancelled.'));
		testAbort = null;
	}
	// The beep and microphone half loads on first use, off the initial page load.
	let ioAudio: typeof import('$lib/player/io-device-test-audio') | null = null;
	async function loadIoAudio(): Promise<typeof import('$lib/player/io-device-test-audio')> {
		ioAudio ??= await import('$lib/player/io-device-test-audio');
		return ioAudio;
	}

	// Closing the panel ends any test: the beep stops and AUDIO IN goes back
	// to opening only when access is already granted.
	$effect(() => {
		if (ioSurface.open) return;
		testBus = null;
		// Only the panel's own Test: its abort cuts its beep, never an agent's.
		abortTest();
	});
	// Unmount also invalidates a Test still loading the beep module.
	$effect(() => () => {
		testToken += 1;
		abortTest();
	});

	// The clock only drives the beep envelope, so it runs while a MASTER or
	// CUE test sounds, not for as long as the panel is open.
	$effect(() => {
		if (!ioSurface.open || testBus === null || testBus === 'input') return;
		let raf = 0;
		const loop = (): void => {
			nowMs = performance.now();
			raf = requestAnimationFrame(loop);
		};
		raf = requestAnimationFrame(loop);
		return () => cancelAnimationFrame(raf);
	});

	$effect(() => {
		// An AUDIO IN Test owns the microphone while it runs; the passive meter
		// steps aside and reopens once the Test lets go.
		if (!ioSurface.open || cueAlignModal.open || testBus === 'input') return;
		const deviceId = headphoneState.selected_input_device_id;
		const label = headphoneState.inputs.find((input) => input.id === deviceId)?.label ?? '';
		// CUEOUT-8393: in the Mac app an open microphone turns MAIN down, so the
		// passive meter stays closed there and says why.
		inputMeterNote = passiveInputMeterBlockedReason(nativeCueSinkAvailable());
		if (inputMeterNote !== null) return;
		void inputRecheck;
		let cancelled = false;
		let stop = (): void => {};
		// Cleanup aborts an open still settling, so the mic never opens after
		// the panel that asked for it has gone.
		const opening = new AbortController();
		void (async () => {
			try {
				const io = await loadIoAudio();
				// Only where access is already granted: opening a microphone that
				// asks permission is an AUDIO IN Test's job, inside its owned run.
				const allowed = await io.microphonePermissionGranted();
				if (cancelled || !allowed) return;
				if (inputLooksLikeHandsfree(label)) return;
				const localStop = await io.startInputLevelWatch(
					deviceId,
					(rms) => {
						if (!cancelled) inputLiveRms = rms;
					},
					opening.signal,
					(reason) => {
						if (!cancelled) inputMeterError = reason;
					}
				);
				if (cancelled) localStop();
				else stop = localStop;
			} catch (error) {
				// A close or a newer open aborted it: nothing failed.
				if (cancelled || opening.signal.aborted) return;
				const reason = error instanceof Error ? error.message : String(error);
				inputMeterError = reason;
				recordPerfEvent('io-input-meter-failed', reason, null, 'warn');
			}
		})();
		return () => {
			cancelled = true;
			opening.abort(new Error('The input meter closed.'));
			stop();
			inputLiveRms = 0;
			inputMeterError = null;
		};
	});

	function testDeviceId(bus: IoTestBus): string | null {
		return ioTestDeviceId(bus, headphoneState);
	}

	async function playDeviceTest(bus: IoTestBus): Promise<void> {
		const mine = ++testToken;
		abortTest();
		const local = new AbortController();
		testAbort = local;
		// Refused before anything opens (AUDIO IN with no input picked); the
		// token bump above already retired any Test still finishing. A missing
		// route session or a preset owning the audio rejects in testrun below.
		testNote = ioTestRefusal(bus, headphoneState);
		if (testNote !== null) {
			testBus = null;
			return;
		}
		testBus = bus;
		// The envelope shows only once the tone is scheduled, never for a
		// test that is still opening its device or has failed.
		testStartedAt = Number.POSITIVE_INFINITY;
		try {
			const io = await loadIoAudio();
			// The panel closed (or another test started) while the module loaded.
			if (mine !== testToken || testBus !== bus) return;
			await testrun(async (_stillCurrent, routeSignal) => {
				// A Test queued behind another closes with the panel: re-checked
				// once the scope is granted, before anything opens.
				if (mine !== testToken || testBus !== bus || !ioSurface.open) return;
				// The route ending aborts the open too, not only this panel.
				if (routeSignal.aborted) local.abort(routeSignal.reason);
				else routeSignal.addEventListener('abort', () => local.abort(routeSignal.reason), { once: true });
				// AUDIO IN opens the picked microphone inside the owned run, and
				// shows its level for as long as the beeps play.
				// Set when the microphone stops sending audio mid-Test.
				let inputEnded = null as string | null;
				const stopInput =
					bus === 'input'
						? await io.startInputLevelWatch(
								headphoneState.selected_input_device_id,
								(rms) => {
									if (mine === testToken) inputLiveRms = rms;
								},
								local.signal,
								(reason) => {
									inputEnded = reason;
								}
							)
						: null;
				try {
					if (mine !== testToken || testBus !== bus || !ioSurface.open) return;
					// A new Test, a close or the route ending cuts this beep, so
					// the replacement never queues behind a tone still sounding.
					const played = await io.playIoTestBeeps(
						bus,
						testDeviceId(bus),
						(startsAtMs) => {
							if (mine === testToken) testStartedAt = startsAtMs;
						},
						local.signal
					);
					// Not played and not cancelled here: the output stalled or was
					// rebuilt mid-pattern (or an agent's Test cut it), so say so.
					if (!played && !local.signal.aborted && mine === testToken) {
						testNote = 'The Test did not finish playing on this output. Check the device, then test again.';
					}
				} finally {
					if (stopInput !== null) {
						stopInput();
						inputRecheck += 1;
					}
				}
				// Read after the stop, which reports a capture that ended unseen.
				if (inputEnded !== null && mine === testToken) testNote = inputEnded;
			});
		} catch (error) {
			if (mine === testToken) testNote = error instanceof Error ? error.message : String(error);
		} finally {
			if (mine === testToken) testBus = null;
			if (testAbort === local) testAbort = null;
		}
	}

	function meterFill(bus: IoTestBus): number {
		const reading = headphoneState.signals[bus];
		const rms = bus === 'input' ? Math.max(reading?.rms ?? 0, inputLiveRms) : reading?.rms;
		const envelope = testBus === bus && bus !== 'input' ? ioTestEnvelope((nowMs - testStartedAt) / 1000) : 0;
		return ioMeterFill(rms, envelope);
	}

	/** What the meter is showing, with the number, for its hover title. */
	function meterText(bus: IoTestBus, fill: number): string {
		const reading = headphoneState.signals[bus];
		const testing = testBus === bus && bus !== 'input' && ioTestEnvelope((nowMs - testStartedAt) / 1000) > 0;
		const source = testing
			? 'test tone level'
			: bus === 'input'
				? 'microphone input level'
				: 'application bus level';
		const rms = bus === 'input' ? Math.max(reading?.rms ?? 0, inputLiveRms) : reading?.rms;
		const measured = !testing && rms !== null && rms !== undefined && rms > 0 ? `, RMS ${rms.toFixed(3)}` : '';
		if (bus === 'input' && inputMeterError !== null && !(rms !== null && rms !== undefined && rms > 0)) {
			return `input meter unavailable: ${inputMeterError}`;
		}
		return `${bus} meter ${Math.round(fill * 100)}%: ${source}${measured}. Physical output not proven.`;
	}
</script>

<svelte:window onkeydown={onWindowKeydown} onpointerdowncapture={onIoOutsidePointerDown} />

<!-- The headphone mark sits inside the MIX and VOL captions ("[icon] MIX"), not beside the dial. -->
{#snippet hpCaptionIcon()}
	<svg class="hp-caption-icon" viewBox="0 0 12 12" width="8" height="8" aria-hidden="true"><path d="M2 8 V6 a4 4 0 0 1 8 0 V8" fill="none" stroke="currentColor" stroke-width="1.2" /><rect x="1.2" y="7" width="2.3" height="3.6" rx="0.8" fill="currentColor" /><rect x="8.5" y="7" width="2.3" height="3.6" rx="0.8" fill="currentColor" /></svg>
{/snippet}

<div class="hp" data-performance-control="headphones">
	<ControlExplainer title="MIX" bullets={mixBullets} demo="headphone-mix" showDelayMs={60}>
		<Knob
			knobId={knobId('hp', 'hp-mix')}
			label="MIX"
			captionLead={hpCaptionIcon}
			accessibleLabel="Headphone CUE to MASTER mix"
			value={headphoneState.mix}
			onchange={onmix}
			onsingleclick={handleMixSingleClick}
			resetValue={0}
			accentColor={headphoneMixAccent(headphoneState.mix)}
		/>
	</ControlExplainer>
	<ControlExplainer title="VOL" bullets={levelBullets} showDelayMs={60}>
		<Knob
			knobId={knobId('hp', 'hp-level')}
			label="VOL"
			captionLead={hpCaptionIcon}
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
	<div class="hp-panel" role="dialog" aria-label="Audio I/O settings" aria-modal="false" tabindex="-1" data-audio-io-panel bind:this={ioPanelEl} onchange={onIoPanelSelectSettled} onfocusout={onIoPanelSelectSettled} onkeydown={onIoPanelKeydown}>
		<header class="hp-panel-header">
			<div>
				<strong>Audio I/O</strong>
				<small>Two clicks outside, Esc, or X to dismiss</small>
			</div>
			<button type="button" class="hp-panel-close" aria-label="Close audio I/O settings" onclick={closeIo}><svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><path d={CLOSE_PATH} fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" /></svg></button>
		</header>
		<div class="hp-panel-body">
			<section class="hp-section" aria-label="Output routing">
				<h3>Routing <ControlExplainer title="Output mode" bullets={modeBullets} demo="headphone-mode" showDelayMs={60}><span class="hp-mode" data-output-mode={headphoneState.output_mode}>{modeLabel}</span></ControlExplainer></h3>
				<div class="hp-mode-choices">
					<ControlExplainer title="ONE OUTPUT" bullets={mainBullets} demo="headphone-practice" showDelayMs={60}><button type="button" aria-label="Practice output mode" aria-pressed={headphoneState.output_mode === 'practice'} onclick={() => onmode('practice')}>One output</button></ControlExplainer>
					<button type="button" aria-label="Two outputs output mode" aria-pressed={headphoneState.output_mode === 'two_outputs'} onclick={() => onmode('two_outputs')}>Two outputs</button>
					<ControlExplainer title="SPLIT L/R" bullets={splitBullets} demo="headphone-split" showDelayMs={60}><button type="button" aria-label="Split cable output mode" aria-pressed={headphoneState.output_mode === 'split_cable'} title="Mono master left, mono cue right on one output: a DJ splitter cable, or MAIN and CUE on the same device" onclick={() => onmode('split_cable')}>Split L/R</button></ControlExplainer>
				</div>
				<p class="hp-context">{modeBullets[headphoneState.output_mode === 'practice' ? 0 : headphoneState.output_mode === 'two_outputs' ? 1 : 2]}</p>
				{#if audioOutputStatus.fallback !== null}<p class="hp-warn" role="status" data-djio-fallback-notice title={djioFallbackTitle(audioOutputStatus.fallback)}>{audioOutputStatus.fallback.message}</p>{/if}
				{#if headphoneState.output_mode === 'split_cable' && headphoneState.split_reason === 'same_device'}
					<p class="hp-context" role="status" data-split-same-device>MAIN and HEADPHONE CUE are the same output, so the room mix plays mono on the left and the cue mono on the right. Pick a different MAIN to get two outputs.</p>
				{:else if headphoneState.output_mode === 'split_cable'}
					<ControlExplainer title="SPLIT warning" bullets={splitBullets} showDelayMs={60}><p class="hp-warn" role="status" title="Split cable wiring">{splitBullets[0]} {splitBullets[1]}</p></ControlExplainer>
				{/if}
			</section>
			<section class="hp-section" aria-label="Future routing options">
				<div class="hp-future"><button type="button" disabled>BOOTH / MONITOR</button><span>Coming soon</span></div>
				<div class="hp-future"><button type="button" disabled>Advanced channel assignment</button><span>Coming soon</span></div>
				{#if remoteSpeakerSection}
					<!-- I18: loaded on demand, so a build with the flag off ships none of it. -->
					{#await remoteSpeakerSection then { default: RemoteSpeakerSection }}<RemoteSpeakerSection />{/await}
				{:else}
					<div class="hp-future"><button type="button" disabled title={plannedTitle('remote-control')}>Remote control</button><span>Coming soon</span></div>
				{/if}
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
					<p class="hp-context">Meters show app-bus or mic level. Test plays a few beeps. Neither proves a speaker you have not heard.</p>
				{#if showError}
					<p class="hp-error" role="alert">{headphoneState.error}</p>
					<button type="button" class="hp-retry" onclick={onacquire}>Retry device access</button>
				{/if}
			</section>
			<SpeakerLinkSection />
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
				<label class="hp-cal-mic">
					<span>Microphone for calibration</span>
					<select
						aria-label="calibration microphone"
						value={calibrationMicId}
						onchange={(event) => chooseCalibrationMic(selectValue(event))}
					>
						<option value="" disabled={calibrationMicId !== ''}>Choose a microphone</option>
						{#each headphoneState.inputs as input (input.id)}
							<option value={input.id}>{input.label || input.id}</option>
						{/each}
					</select>
				</label>
				{#if headphoneState.inputs.length === 0}
					<p class="hp-warn" role="status">No microphones are listed. Allow microphone access, then choose the mic that can hear the speakers. A loud room on the wrong mic still reads as silence.</p>
					<button type="button" onclick={onacquire}>Allow microphone access</button>
				{:else if calibrationMicId === ''}
					<p class="hp-context">Choose a microphone before starting. Calibration will not run until you do.</p>
				{:else}
					<p class="hp-context">Calibration listens on this microphone only.</p>
				{/if}
				<button type="button" class="hp-calibrate-button" aria-label="CALIBRATE CUE ALIGNMENT" data-performance-control="cue-calibrate" disabled={!calibrateEnabled} onclick={oncalibrate}>CALIBRATE</button>
				<p class="hp-context">{calibrateBullets[0]}</p>
				{#if cueAlignModal.open}
					<CueAlignModal
						headphones={headphoneState}
						onmode={onAlignmentMode}
						embedded={true}
						calibrationMicId={calibrationMicId}
						onPickMic={chooseCalibrationMic}
					/>
				{/if}
			</section>
		</div>
	</div>
{/if}

{#snippet outputMenu()}
	<div class="hp-menu">
		<ControlExplainer title="MASTER / MAIN" bullets={masterPickBullets} showDelayMs={40} placement="right">
			<span class="hp-pick">
				<span class="hp-pick-name">MASTER / MAIN</span>
				<span class="hp-pick-row">
					<select
						aria-label="master output device"
						value={headphoneState.selected_master_output_device_id ?? ''}
						disabled={!headphoneState.supported || !access.output_pinning}
						onchange={(event) => pickThenMirror(event.currentTarget, onmaster, () => headphoneState.selected_master_output_device_id)}
					>
						<option value="" disabled>choose MAIN</option>
						{#each headphoneState.outputs as output (output.id)}
							<option value={output.id}>{outputOptionLabel(output)}</option>
						{/each}
					</select>
					{@render testAndMeter('master', 'master output')}
				</span>
			</span>
		</ControlExplainer>
		<ControlExplainer title="HEADPHONE CUE" bullets={cuePickBullets} showDelayMs={40} placement="right">
			<span class="hp-pick">
				<span class="hp-pick-name">HEADPHONE CUE</span>
				<span class="hp-pick-row">
					<select
						aria-label="headphone output device"
						value={headphoneState.selected_output_device_id ?? ''}
						disabled={!headphoneState.supported || !access.output_pinning}
						onchange={(event) => pickThenMirror(event.currentTarget, onselect, () => headphoneState.selected_output_device_id)}
					>
						<option value="" disabled>{cuePlaceholder}</option>
						{#each headphoneState.outputs as output (output.id)}
							<option value={output.id}>{outputOptionLabel(output)}</option>
						{/each}
					</select>
					{@render testAndMeter('cue', 'headphone output')}
				</span>
			</span>
		</ControlExplainer>
		{#if wirelessMain !== null}<p class="hp-context" data-wireless-main-note>{wirelessMain}</p>{/if}
		{#if selectedLabel !== null && monitorLabelIsBluetooth(selectedLabel)}<p class="hp-context">Bluetooth CUE can drift against MASTER. Use CALIBRATE for a measured starting offset.</p>{/if}
		{#if livenessAlert !== null}<p class="hp-liveness-alert" role="alert" data-headphone-liveness-alert>{livenessAlert}</p>{/if}
		<ControlExplainer title="AUDIO IN" bullets={inputPickBullets} showDelayMs={40} placement="right">
			<span class="hp-pick">
				<span class="hp-pick-name">AUDIO IN</span>
				<span class="hp-pick-row">
					<select
						aria-label="audio input device"
						value={audioInValue}
						onchange={(event) => pickThenMirror(event.currentTarget, chooseCalibrationMic, () => headphoneState.selected_input_device_id)}
					>
						<option value="" disabled={audioInValue !== ''}>choose input</option>
						{#each headphoneState.inputs as input (input.id)}
							<option value={input.id}>{input.label || input.id}</option>
						{/each}
					</select>
					{@render testAndMeter('input', 'audio input')}
				</span>
			</span>
		</ControlExplainer>
		{#if headphoneState.inputs.length === 0}
			<p class="hp-warn" role="status">AUDIO IN has no microphones yet. Allow microphone access, then pick one. The menu stays empty until that grant.</p>
		{/if}
		{#if testNote !== null}<p class="hp-context" role="status">{testNote}</p>{/if}
		{#if inputMeterNote !== null}<p class="hp-context" role="status" data-input-meter-note>{inputMeterNote}</p>{/if}
		{#if inputMeterError !== null}<p class="hp-context" role="status" data-input-meter-error>Live input meter unavailable: {inputMeterError}</p>{/if}
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

{#snippet testAndMeter(bus: IoTestBus, label: string)}
	<button
		type="button"
		class="hp-io-test"
		aria-label={`Test ${label}`}
		aria-busy={testBus === bus}
		onclick={(event) => {
			event.preventDefault();
			event.stopPropagation();
			void playDeviceTest(bus);
		}}>Test</button
	>
	{@render volumeMeter(bus)}
{/snippet}

{#snippet volumeMeter(bus: IoTestBus)}
	{@const reading = headphoneState.signals[bus]}
	{@const fill = meterFill(bus)}
	<span
		class="hp-vmeter"
		class:hot={fill >= 0.9}
		role="meter"
		data-signal-bus={bus}
		data-signal-state={reading?.state ?? 'unavailable'}
		aria-valuemin={0}
		aria-valuemax={100}
		aria-valuenow={Math.round(fill * 100)}
		aria-label={meterText(bus, fill)}
		title={meterText(bus, fill)}
	><span class="hp-vmeter-fill" style:height={`${Math.round(fill * 100)}%`}></span></span>
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
	/* No device: the dim text token alone says idle. A 0.6 opacity on top of it
	   rendered 7px text at 2.8:1 (alpha status B25). */
	.midi-btn.st-grey {
		color: var(--rb-text-dim, #838990);
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
		/* Breathing room from the VOL knob (the maintainer, Mon 5 Oct 2026). */
		margin-left: 10px;
	}
	.hp-btn-io.io-alert {
		position: relative;
		/* The border and glow carry the alarm red; the 10px label is lifted toward
		   white so it reads at 4.5:1 on the dark panel (raw red was 3.7:1, B25). */
		color: color-mix(in srgb, var(--rb-red, #e5484d) 70%, #fff);
		border-color: var(--rb-red, #e5484d);
		box-shadow: 0 0 3px color-mix(in srgb, var(--rb-red, #e5484d) 45%, transparent);
	}
	/* The pulse animates the OPACITY of a stronger glow layer, not box-shadow
	   itself: an animated box-shadow repaints on the main thread every frame
	   for as long as the alert stands, which cost frames with two decks
	   playing (Fri 9 Oct 2026). Opacity runs on the compositor. */
	.hp-btn-io.io-alert::after {
		content: '';
		position: absolute;
		inset: -1px;
		border-radius: inherit;
		box-shadow: 0 0 9px color-mix(in srgb, var(--rb-red, #e5484d) 95%, transparent);
		opacity: 0;
		pointer-events: none;
		animation: io-alert-pulse 1.2s ease-in-out infinite;
	}
	@keyframes io-alert-pulse {
		0%,
		100% {
			opacity: 0;
		}
		50% {
			opacity: 1;
		}
	}
	/* Light skin: the panel is light, so the plain red already contrasts. */
	/* Light surfaces keep the raw skin red: lifting it toward white there
	   drops it to 3.1:1 (ui-contrast-audit, theme-light). The light theme is
	   a light surface only under the Default skin (no data-skin); Gothic stays
	   dark over either theme. */
	:global(html:is([data-skin='light'], [data-theme='light']:not([data-skin]))) .hp-btn-io.io-alert {
		color: var(--rb-red, #b3261e);
	}
	@media (prefers-reduced-motion: reduce) {
		/* Glow without pulse. */
		.hp-btn-io.io-alert::after {
			animation: none;
			opacity: 0.5;
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
	/* Same leftover width on every row: the Test button and the meter are fixed. */
	.hp-pick-row {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto auto;
		gap: 6px;
		align-items: stretch;
		width: 100%;
	}
	.hp-pick-row select {
		width: 100%;
		min-width: 0;
		max-width: none;
		box-sizing: border-box;
	}
	.hp-section .hp-io-test {
		font: inherit;
		font-size: 10px;
		letter-spacing: 0.04em;
		padding: 0 6px;
		white-space: nowrap;
		background: var(--rb-panel-raised, #1a1e25);
		border: 1px solid var(--rb-border, #333);
		border-radius: 3px;
		color: var(--rb-text, #c8cdd2);
		cursor: pointer;
	}
	.hp-section .hp-io-test[aria-busy='true'] {
		border-color: var(--rb-accent, #4fb3ff);
		color: var(--rb-accent, #4fb3ff);
	}
	.hp-vmeter {
		flex: 0 0 auto;
		width: 4px;
		min-height: 18px;
		align-self: stretch;
		position: relative;
		overflow: hidden;
		background: #12151a;
		border: 1px solid var(--rb-border, #333);
		border-radius: 1px;
	}
	.hp-vmeter-fill {
		position: absolute;
		left: 0;
		right: 0;
		bottom: 0;
		background: var(--rb-green, #35c04f);
	}
	.hp-vmeter.hot .hp-vmeter-fill {
		background: var(--rb-red, #e5484d);
	}
	.hp-cal-mic {
		display: flex;
		flex-direction: column;
		gap: 4px;
		margin: 0 0 8px;
		font-size: 10px;
		letter-spacing: 0.04em;
		color: var(--rb-text-dim, #838990);
	}
	.hp-cal-mic select {
		width: 100%;
		max-width: none;
		font-size: 12px;
		padding: 4px 6px;
		box-sizing: border-box;
	}
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
