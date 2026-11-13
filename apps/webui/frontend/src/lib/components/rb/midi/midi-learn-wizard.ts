/**
 * Guided setup for an unmapped controller (issue #10071, PRD-CTRL-01).
 *
 * The wizard records unshifted bindings only, so a collision is a match on
 * (ch, kind, id) among unshifted records.
 *
 * Pure state module: walks a fixed action list one prompt at a time, records
 * the next message from the chosen device as the binding for that prompt, and
 * finishes into an installed-map document (the PUT /api/v1/midi/maps/{id}
 * payload). No runes, no WebMIDI, no fetch, so the stepper can be driven
 * without a component (agent parity) and unit tested under plain node.
 *
 * Prompt order mimics Mixxx's MIDI learning wizard and
 * (private path) section 3.4: deck 1 then deck 2 transport,
 * then mixer per channel, then globals, browse, load and shift.
 *
 * Requirements (mini-PRD):
 *   - GUIDED_SETUP_STEPS is the exported fixed prompt order.
 *     [if] a step's action is not a valid MidiAction [then] broken
 *   - wizardCapture only accepts messages from the wizard's own device.
 *     [if] another port's message arms a binding [then] broken
 *   - Confirm records provenance {tier: 'learned', cite, verified: true}.
 *   - A capture whose (ch, kind, id, shift) is already recorded is refused
 *     naming both labels.
 *     [if] a colliding binding reaches the document [then] broken
 *   - Skip leaves the step unbound; Retry re-arms the same step.
 */

import type { DeckId } from '$lib/rb/deck-slots';
import type { BindingProvenance, InstalledMapDoc } from '$lib/rb/midi/installed-maps';
import type { LearnLogEntry, MidiAction, MidiBinding, MidiSource } from '$lib/rb/midi/midi-types';

/** One guided-setup prompt: what to touch and what it binds to. */
export interface GuidedSetupStep {
	label: string;
	action: MidiAction;
}

/** Deck transport prompts, in the fixed per-deck order. */
function _deckSteps(deck: DeckId): GuidedSetupStep[] {
	const d = `Deck ${deck}`;
	return [
		{ label: `${d} play`, action: { type: 'deck_play_toggle', deck } },
		{ label: `${d} cue`, action: { type: 'deck_cue', deck } },
		{ label: `${d} sync`, action: { type: 'deck_sync_toggle', deck } },
		{ label: `${d} hot cue A`, action: { type: 'deck_hot_cue', deck, slot: 'A' } },
		{ label: `${d} hot cue B`, action: { type: 'deck_hot_cue', deck, slot: 'B' } },
		{ label: `${d} hot cue C`, action: { type: 'deck_hot_cue', deck, slot: 'C' } },
		{ label: `${d} hot cue D`, action: { type: 'deck_hot_cue', deck, slot: 'D' } },
		{ label: `${d} auto loop`, action: { type: 'deck_auto_loop_toggle', deck } },
		{ label: `${d} loop exit`, action: { type: 'deck_loop_exit', deck } },
		{ label: `${d} pitch fader`, action: { type: 'deck_pitch', deck, lsbOffset: null } },
		{ label: `${d} jog wheel`, action: { type: 'deck_jog', deck } }
	];
}

/** Mixer channel-strip prompts, in the fixed per-channel order. */
function _mixerSteps(deck: DeckId): GuidedSetupStep[] {
	const c = `Channel ${deck}`;
	return [
		{ label: `${c} trim`, action: { type: 'mixer_channel', deck, target: 'trim' } },
		{ label: `${c} EQ high`, action: { type: 'mixer_channel', deck, target: 'eq', band: 'high' } },
		{ label: `${c} EQ mid`, action: { type: 'mixer_channel', deck, target: 'eq', band: 'mid' } },
		{ label: `${c} EQ low`, action: { type: 'mixer_channel', deck, target: 'eq', band: 'low' } },
		{ label: `${c} filter`, action: { type: 'mixer_channel', deck, target: 'filter' } },
		{ label: `${c} fader`, action: { type: 'mixer_channel', deck, target: 'fader' } },
		{ label: `${c} cue`, action: { type: 'channel_cue', deck } }
	];
}

/** The fixed prompt order for a two-deck controller. */
export const GUIDED_SETUP_STEPS: readonly GuidedSetupStep[] = Object.freeze([
	..._deckSteps(1),
	..._deckSteps(2),
	..._mixerSteps(1),
	..._mixerSteps(2),
	{ label: 'Crossfader', action: { type: 'mixer_global', target: 'crossfader' } },
	{ label: 'Master level', action: { type: 'mixer_global', target: 'master' } },
	{ label: 'Headphone mix', action: { type: 'headphone_mix' } },
	{ label: 'Headphone level', action: { type: 'headphone_level' } },
	{ label: 'Browse encoder', action: { type: 'browse_encoder' } },
	{ label: 'Load deck 1', action: { type: 'browse_load', deck: 1 } },
	{ label: 'Load deck 2', action: { type: 'browse_load', deck: 2 } },
	{ label: 'Shift', action: { type: 'shift_modifier' } }
]);

/** The MIDI port the wizard is mapping. */
export interface GuidedSetupPort {
	deviceId: string;
	name: string;
	manufacturer: string;
}

/** A captured message awaiting Confirm, shown as bytes plus decode. */
export interface GuidedSetupCapture {
	bytes: [number, number, number];
	source: MidiSource;
}

/** One recorded binding, with the prompt label it answered. */
export interface GuidedSetupRecord {
	stepIndex: number;
	label: string;
	binding: MidiBinding & { provenance: BindingProvenance };
}

export interface GuidedSetupSession {
	port: GuidedSetupPort;
	startedAt: string;
	stepIndex: number;
	capture: GuidedSetupCapture | null;
	/** Set when the last capture collided with a recorded binding. */
	refused: string | null;
	records: GuidedSetupRecord[];
	skipped: number[];
	finished: boolean;
}

/** Start a guided setup for one unmapped port; `now` is the ISO start time. */
export function wizardStart(port: GuidedSetupPort, now: string): GuidedSetupSession {
	return {
		port,
		startedAt: now,
		stepIndex: 0,
		capture: null,
		refused: null,
		records: [],
		skipped: [],
		finished: false
	};
}

/** The prompt the session is on, or null once every step is answered. */
export function wizardStep(s: GuidedSetupSession): GuidedSetupStep | null {
	return GUIDED_SETUP_STEPS[s.stepIndex] ?? null;
}

/** Read-only snapshot of the session for a component or an agent. */
export function wizardState(s: GuidedSetupSession): Readonly<GuidedSetupSession> & {
	step: GuidedSetupStep | null;
	total: number;
} {
	return {
		...s,
		records: [...s.records],
		skipped: [...s.skipped],
		step: wizardStep(s),
		total: GUIDED_SETUP_STEPS.length
	};
}

/** Feed one learn-log entry; the first decodable message from the wizard's
 * device arms the current step. Returns true when it was taken. */
export function wizardCapture(s: GuidedSetupSession, entry: LearnLogEntry): boolean {
	if (s.finished || s.capture !== null || wizardStep(s) === null) return false;
	if (entry.deviceId !== s.port.deviceId || entry.decoded === null) return false;
	const source = entry.decoded;
	const clash = s.records.find(
		(r) =>
			r.binding.source.ch === source.ch &&
			r.binding.source.kind === source.kind &&
			r.binding.source.id === source.id &&
			r.binding.shift !== true
	);
	if (clash !== undefined) {
		s.refused = `${wizardStep(s)?.label} uses the same control as ${clash.label}; move a different control`;
		return false;
	}
	s.refused = null;
	s.capture = { bytes: [entry.status, entry.data1, entry.data2], source };
	return true;
}

/** Wire flags a prompt implies: jog and browse turn as two's-complement encoders. */
function _flagsFor(action: MidiAction): Pick<MidiBinding, 'relative'> {
	return action.type === 'deck_jog' || action.type === 'browse_encoder' ? { relative: true } : {};
}

/** Record the armed capture as the current step's binding and advance. */
export function wizardConfirm(s: GuidedSetupSession): boolean {
	const step = wizardStep(s);
	if (s.finished || step === null || s.capture === null) return false;
	s.records.push({
		stepIndex: s.stepIndex,
		label: step.label,
		binding: {
			source: { ...s.capture.source },
			action: step.action,
			..._flagsFor(step.action),
			provenance: {
				tier: 'learned',
				cite: `guided setup ${s.startedAt}, port ${s.port.name}`,
				verified: true
			}
		}
	});
	s.capture = null;
	s.stepIndex += 1;
	return true;
}

/** Leave the current step unbound and advance. */
export function wizardSkip(s: GuidedSetupSession): boolean {
	if (s.finished || wizardStep(s) === null) return false;
	s.skipped.push(s.stepIndex);
	s.capture = null;
	s.refused = null;
	s.stepIndex += 1;
	return true;
}

/** Drop the armed capture so the next message re-arms the same step. */
export function wizardRetry(s: GuidedSetupSession): void {
	s.capture = null;
	s.refused = null;
}

/** Lowercase dash slug the daemon accepts as a map id. */
export function guidedSetupMapId(portName: string): string {
	const slug = portName
		.toLowerCase()
		.replace(/[^a-z0-9]+/g, '-')
		.replace(/^-+|-+$/g, '');
	return slug.length > 0 ? slug : 'controller';
}

/** Escape a port name so it matches literally as a nameMatch pattern. */
export function escapePortName(portName: string): string {
	return portName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** Finish the session into the installed-map document to PUT. */
export function wizardFinish(s: GuidedSetupSession): InstalledMapDoc {
	if (s.records.length === 0) {
		throw new Error('wizardFinish: no binding was recorded; nothing to save');
	}
	s.finished = true;
	s.capture = null;
	return {
		schemaVersion: 1,
		id: guidedSetupMapId(s.port.name),
		vendor: s.port.manufacturer.trim() || 'Unknown',
		model: s.port.name,
		nameMatch: escapePortName(s.port.name),
		bindings: s.records.map((r) => r.binding)
	};
}
