/**
 * SET OUTPUTS (the I/O button in the mixer headphone row, `.hp`).
 *
 * Pin 894af5672c3b (JIK, Wed 16 Sep 2026): the I/O hover must vanish the
 * instant the pointer leaves, carry one sentence plus a compact list of the
 * devices in use plus "Click I/O to set audio outputs", and the button must
 * pulse and glow red until it has been clicked once this session when the
 * outputs are not set.
 *
 * Pure helpers only, so the rules are unit-testable without a DOM.
 */
import type { HeadphoneOutputDevice, HeadphoneState } from './mixer-types';

/** sessionStorage key: the user has clicked SET OUTPUTS at least once this session. */
export const IO_OUTPUTS_CLICKED_SESSION_KEY = 'mdt.io-set-outputs.clicked';

export const IO_HOVER_SENTENCE = 'Routes the room mix and your headphone cue to audio devices.';
export const IO_HOVER_CTA = 'Click I/O to set audio outputs';

type OutputsSlice = Pick<
	HeadphoneState,
	| 'output_mode'
	| 'outputs'
	| 'inputs'
	| 'selected_master_output_device_id'
	| 'selected_output_device_id'
	| 'selected_input_device_id'
>;

/**
 * "Not set" means the user has chosen NEITHER a MASTER / MAIN room sink NOR a
 * HEADPHONE CUE sink: the room follows whatever the OS default is (which a
 * pair of headphones plugged in can steal) and there is no cue device. Either
 * explicit choice counts as set, because each is a deliberate routing act.
 * Read from existing HeadphoneState only; no device enumeration happens here.
 */
export function outputsUnset(state: Pick<OutputsSlice, 'selected_master_output_device_id' | 'selected_output_device_id'>): boolean {
	return state.selected_master_output_device_id === null && state.selected_output_device_id === null;
}

/** Pulse and glow red only while unset AND not yet clicked this session. */
export function ioShouldAlert(unset: boolean, clickedThisSession: boolean): boolean {
	return unset && !clickedThisSession;
}

type SessionStore = Pick<Storage, 'getItem' | 'setItem'>;

function _store(storage?: SessionStore | null): SessionStore | null {
	if (storage !== undefined) return storage;
	try {
		return typeof sessionStorage === 'undefined' ? null : sessionStorage;
	} catch {
		return null;
	}
}

/** True when SET OUTPUTS was clicked earlier this session. Storage failure reads as not clicked. */
export function readIoClickedThisSession(storage?: SessionStore | null): boolean {
	try {
		return _store(storage)?.getItem(IO_OUTPUTS_CLICKED_SESSION_KEY) === '1';
	} catch {
		return false;
	}
}

/** Remember the click for the session; a blocked store is ignored (the in-memory flag still holds). */
export function markIoClickedThisSession(storage?: SessionStore | null): void {
	try {
		_store(storage)?.setItem(IO_OUTPUTS_CLICKED_SESSION_KEY, '1');
	} catch {
		// private window / blocked site data: the component's own state still stops the pulse
	}
}

function _label(devices: readonly HeadphoneOutputDevice[], id: string | null): string | null {
	if (id === null) return null;
	const hit = devices.find((d) => d.id === id);
	// A pinned id whose label is not enumerated yet (labels need the I/O gesture)
	// is still a choice; say so rather than pretend it is the OS default.
	return hit?.label || 'chosen device (name not loaded yet)';
}

export interface IoHoverSummary {
	sentence: string;
	devices: string[];
	cta: string;
}

/** Compact hover: one sentence, which devices are in use, and the call to action. */
export function ioHoverSummary(state: OutputsSlice): IoHoverSummary {
	const master = _label(state.outputs, state.selected_master_output_device_id) ?? 'OS default output';
	const cue = _label(state.outputs, state.selected_output_device_id);
	const input = _label(state.inputs, state.selected_input_device_id);
	const devices = [`MAIN: ${master}`];
	if (state.output_mode === 'split_cable') devices.push('CUE: right leg of MAIN (split cable)');
	else devices.push(`CUE: ${cue ?? (state.output_mode === 'practice' ? 'blended into MAIN (practice)' : 'none')}`);
	if (input !== null) devices.push(`IN: ${input}`);
	return { sentence: IO_HOVER_SENTENCE, devices, cta: IO_HOVER_CTA };
}
