/**
 * WebMIDI runtime for the P0 controller build (build unit: midi core).
 * Rune module - MUST stay .svelte.ts (rune_outside_svelte otherwise,
 * same bug class as stores.svelte.ts / audio-engine.svelte.ts).
 *
 * Grounding: SPIKE-CONTROLLERS.md section 2 - both target controllers are
 * class-compliant plain Note/CC with no SysEx, so this module requests
 * MIDI access with sysex: false and decodes exactly three families:
 * Note On/Off, Control Change, Pitch Bend. Chrome gates all WebMIDI behind
 * a permission prompt (spike 2a, sources 25/26); Safari has no WebMIDI at
 * all (spike 2c, source 28) - surfaced as permission === 'unsupported'.
 *
 * Requirements (mini-PRD):
 *   ✔︎ initMidi(): navigator.requestMIDIAccess({ sysex: false });
 *     permission $state walks prompt -> granted | denied; missing API ->
 *     unsupported. Fail-fast: init on an unsupported browser throws.
 *     [if] initMidi() on Safari [then ⛔️] permission 'unsupported' + throw
 *     [if] user denies the Chrome prompt [then] permission 'denied', no
 *       silent retry loop
 *   ✔︎ 🎯 webMidiSupported(): the page knows up front whether it can ask at
 *     all, so a WebMIDI-less window (Safari, the macOS desktop app's
 *     WKWebView) starts 'unsupported' and never offers a request.
 *     [if] the desktop app shows "not requested yet" and a request button
 *       [then ⛔️] broken
 *   ✔︎ Device discovery + hot-plug: statechange rescans; each input is
 *     resolved against registered DeviceMaps by nameMatch regex; inputs
 *     with no map still get a listener so their traffic learn-logs.
 *     [if] a controller is plugged in mid-session [then] it appears in
 *       midiState.devices and starts dispatching without a reload
 *   ✔︎ Dispatch: [status,d1,d2] -> MidiSource decode -> binding lookup
 *     (shift layer first, unshifted fallback) -> handler emit. EVERY
 *     inbound message lands in the learn log, mapped or not.
 *     [if] a message arrives with no matching binding [then] a learn-log
 *       entry with mapped false + reason, NEVER a silent drop ⛔️
 *   ✔︎ 14-bit CC pairing (deck_pitch lsbOffset): MSB stored, emit fires on
 *     LSB completing the pair (standard MSB-then-LSB order, spike 2a).
 *     [if] MSB 0x40 then LSB 0x00 arrive [then] one continuous14 emit with
 *       raw 8192
 *   ✔︎ Shift layer: shift_modifier bindings flip midiState.shiftHeld on
 *     press/release; shifted bindings win while held.
 *   ✔︎ Learn log: rolling $state ring buffer, newest first, capped at
 *     LEARN_LOG_CAP (50).
 *   ✔︎ LED sender: per-device coalescing queue flushed on a throttle
 *     timer; last write per (ch,note) wins within a flush window.
 */

import type {
	DeviceMap,
	LearnLogEntry,
	MidiAction,
	MidiBinding,
	MidiInputValue,
	MidiSource
} from '$lib/rb/midi/midi-types';
import {
	bindingKey as _bindingKey,
	chKey as _chKey,
	combine14,
	decodeRelative,
	decodeSource
} from '$lib/rb/midi/decode';

// The pure wire decoders live in decode.ts (webmidi crossed the 600-line file
// limit). Re-exported here so callers and tests keep their import path.
export { combine14, decodeRelative, decodeSource };

// -------------------------------------------------------------- constants

export const LEARN_LOG_CAP = 50;
/** LED queue flush interval. Coalesces bursts (e.g. hot-cue bank refresh)
 * into one send per (ch,note) per window. */
export const LED_THROTTLE_MS = 15;

/** Wire status nibbles for the outbound queue. Queued writes are keyed by
 * status so Note-On LED writes and CC meter writes share one coalescing
 * flush without ever being confused for each other. */
export const MIDI_STATUS_NOTE_ON = 0x90;
export const MIDI_STATUS_CC = 0xb0;

// ------------------------------------------------------------ rune stores

export type MidiPermission = 'unsupported' | 'prompt' | 'granted' | 'denied';

/** One connected MIDI device as the UI + glue see it. */
export interface MidiDeviceInfo {
	/** WebMIDI input port id (primary identity for dispatch + LEDs). */
	id: string;
	name: string;
	manufacturer: string;
	/** vendor of the resolved DeviceMap; null = no map matched (learn-log
	 * only device). */
	mapVendor: string | null;
	/** True when a same-named output port exists (LED feedback possible). */
	hasOutput: boolean;
}

/** True when this page can call navigator.requestMIDIAccess at all. Chrome,
 * Edge, Electron and WebView2 can. Safari and WKWebView (the macOS desktop
 * app) cannot: WebKit has no WebMIDI and has said it will not ship one, so
 * a request there can never succeed and no OS prompt exists to grant it. */
export function webMidiSupported(): boolean {
	return typeof navigator !== 'undefined' && navigator.requestMIDIAccess !== undefined;
}

/** Reactive WebMIDI surface state. */
export const midiState: {
	permission: MidiPermission;
	devices: MidiDeviceInfo[];
	shiftHeld: boolean;
} = $state({
	permission: webMidiSupported() ? 'prompt' : 'unsupported',
	devices: [],
	shiftHeld: false
});

/** Rolling learn log, newest first. THE debugging tool for writing device
 * maps: unmapped traffic is captured here, never dropped. */
export const learnLog: LearnLogEntry[] = $state([]);

// -------------------------------------------------- non-reactive runtime

interface _ResolvedDevice {
	input: MIDIInput;
	output: MIDIOutput | null;
	map: DeviceMap | null;
	/** binding lookup key -> binding (see _bindingKey). */
	index: Map<string, MidiBinding>;
	/** 14-bit pairing state per MSB controller: `${ch}:${msbId}` -> value. */
	msbValues: Map<string, number>;
	/** LSB controller reverse index: `${ch}:${lsbId}` -> MSB binding. */
	lsbIndex: Map<string, MidiBinding>;
}

let _access: MIDIAccess | null = null;
/** Where a registered map came from. Precedence is TIERED, not array order:
 * an 'installed' map (onboarded at runtime and persisted by the daemon) always
 * beats the 'builtin' map compiled into the bundle, whichever registered
 * first. Order-dependent precedence would make resolution depend on module
 * evaluation order, which nothing here controls. */
export type DeviceMapTier = 'builtin' | 'installed';

export interface RegisteredDeviceMap {
	map: DeviceMap;
	tier: DeviceMapTier;
}

const _deviceMaps: RegisteredDeviceMap[] = [];
const _resolved: Map<string, _ResolvedDevice> = new Map();
let _actionHandler:
	| ((action: MidiAction, value: MidiInputValue, deviceId: string, pressT0Ms: number) => void)
	| null =
	null;

// LED queue: deviceId -> (`${ch}:${note}` -> velocity), flushed on a timer.
const _ledQueues: Map<string, Map<string, number>> = new Map();
let _ledTimer: ReturnType<typeof setInterval> | null = null;

// ---------------------------------------------------------------- _helpers



function _pushLearnLog(entry: LearnLogEntry): void {
	learnLog.unshift(entry);
	if (learnLog.length > LEARN_LOG_CAP) learnLog.length = LEARN_LOG_CAP;
}

function _buildIndex(map: DeviceMap): {
	index: Map<string, MidiBinding>;
	lsbIndex: Map<string, MidiBinding>;
} {
	const index = new Map<string, MidiBinding>();
	const lsbIndex = new Map<string, MidiBinding>();
	for (const b of map.bindings) {
		const key = _bindingKey(b.shift === true, b.source);
		if (index.has(key)) {
			throw new Error(
				`DeviceMap ${map.vendor}: duplicate binding for ${key} - maps must be unambiguous`
			);
		}
		index.set(key, b);
		if (b.action.type === 'deck_pitch' && b.action.lsbOffset !== null) {
			if (b.source.kind !== 'cc') {
				throw new Error(`DeviceMap ${map.vendor}: 14-bit deck_pitch must bind a cc source`);
			}
			lsbIndex.set(_chKey(b.source.ch, b.source.id + b.action.lsbOffset), b);
		}
	}
	return { index, lsbIndex };
}

function _resolveMap(portName: string): DeviceMap | null {
	// Installed first, then builtin: see DeviceMapTier.
	for (const { map } of [
		..._deviceMaps.filter((e) => e.tier === 'installed'),
		..._deviceMaps.filter((e) => e.tier === 'builtin')
	]) {
		if (new RegExp(map.nameMatch, 'i').test(portName)) return map;
	}
	return null;
}

function _findOutputFor(access: MIDIAccess, inputName: string): MIDIOutput | null {
	for (const output of access.outputs.values()) {
		if (output.name === inputName) return output;
	}
	return null;
}

function _rescanPorts(): void {
	if (_access === null) throw new Error('_rescanPorts before initMidi()');
	const seen = new Set<string>();
	for (const input of _access.inputs.values()) {
		if (input.state !== 'connected') continue;
		seen.add(input.id);
		if (_resolved.has(input.id)) continue;
		const name = input.name ?? '';
		const map = _resolveMap(name);
		const { index, lsbIndex } =
			map !== null
				? _buildIndex(map)
				: { index: new Map<string, MidiBinding>(), lsbIndex: new Map<string, MidiBinding>() };
		const device: _ResolvedDevice = {
			input,
			output: _findOutputFor(_access, name),
			map,
			index,
			msbValues: new Map(),
			lsbIndex
		};
		input.onmidimessage = (ev: MIDIMessageEvent) => _dispatch(device, ev);
		_resolved.set(input.id, device);
	}
	for (const id of [..._resolved.keys()]) {
		if (!seen.has(id)) {
			const dev = _resolved.get(id);
			if (dev !== undefined) dev.input.onmidimessage = null;
			_resolved.delete(id);
			_ledQueues.delete(id);
		}
	}
	midiState.devices = [..._resolved.values()].map((d) => ({
		id: d.input.id,
		name: d.input.name ?? '',
		manufacturer: d.input.manufacturer ?? '',
		mapVendor: d.map?.vendor ?? null,
		hasOutput: d.output !== null
	}));
}

function _valueFor(binding: MidiBinding, src: MidiSource, status: number, d2: number): MidiInputValue {
	if (src.kind === 'note') {
		const pressed = (status & 0xf0) === 0x90 && d2 > 0;
		return { kind: 'button', pressed, velocity: d2 };
	}
	if (src.kind === 'pitchbend') {
		// 14-bit in one message: d1 = LSB, d2 = MSB (handled by caller raw).
		throw new Error('pitchbend values are built in _dispatch, not _valueFor');
	}
	if (binding.relative === true) {
		return { kind: 'relative', delta: decodeRelative(d2) };
	}
	const value01 = d2 / 127;
	return { kind: 'continuous', value01: binding.invert === true ? 1 - value01 : value01, raw: d2 };
}

function _emit(
	device: _ResolvedDevice,
	binding: MidiBinding,
	value: MidiInputValue,
	log: Omit<LearnLogEntry, 'mapped' | 'note'>
): void {
	// Shift modifier is runtime-internal state, not a glue action.
	if (binding.action.type === 'shift_modifier') {
		if (value.kind !== 'button') {
			throw new Error('shift_modifier must be bound to a note (button) source');
		}
		midiState.shiftHeld = value.pressed;
		_pushLearnLog({
			...log,
			mapped: true,
			note: `shift ${value.pressed ? 'held' : 'released'}`,
			action: binding.action
		});
		return;
	}
	if (_actionHandler === null) {
		// Loud, not silent: mapping matched but nothing is wired to act.
		_pushLearnLog({
			...log,
			mapped: false,
			note: `no action handler registered (${binding.action.type})`,
			action: binding.action
		});
		console.error('[midi] action arrived before registerActionHandler()', binding.action);
		return;
	}
	try {
		// `log.ts` is `performance.now()` taken at message RECEIPT, at the top of
		// `_dispatch`, so it is already the controller press stamp on the epoch
		// the perf ring uses. A hardware press is the P0 gesture the latency
		// program exists for; without this it filed a plain schedule row and the
		// primary control surface went unmeasured.
		_actionHandler(binding.action, value, device.input.id, log.ts);
	} catch (exc) {
		// Loud fail-fast: the message still lands in the learn log, the error
		// still propagates (no silent swallow).
		_pushLearnLog({
			...log,
			mapped: false,
			note: `handler error: ${String(exc)}`,
			action: binding.action
		});
		console.error('[midi] action handler threw', binding.action, exc);
		throw exc;
	}
	_pushLearnLog({ ...log, mapped: true, note: binding.action.type, action: binding.action });
}

function _dispatch(device: _ResolvedDevice, ev: MIDIMessageEvent): void {
	const data = ev.data;
	if (data === null || data.length === 0) return;
	const status = data[0];
	if (status >= 0xf8) return; // realtime clock ticks: pure noise, not logged
	const d1 = data.length > 1 ? data[1] : 0;
	const d2 = data.length > 2 ? data[2] : 0;
	const src = decodeSource(status, d1);
	const log: Omit<LearnLogEntry, 'mapped' | 'note'> = {
		ts: performance.now(),
		deviceId: device.input.id,
		deviceName: device.input.name ?? '',
		status,
		data1: d1,
		data2: d2,
		decoded: src
	};
	if (src === null) {
		_pushLearnLog({ ...log, mapped: false, note: 'undecoded status family (out of P0 scope)' });
		return;
	}
	if (device.map === null) {
		_pushLearnLog({ ...log, mapped: false, note: 'no device map matched this port name' });
		return;
	}
	// 14-bit LSB leg: pairs with a stored MSB, emits the combined value.
	if (src.kind === 'cc') {
		const lsbBinding = device.lsbIndex.get(_chKey(src.ch, src.id));
		if (lsbBinding !== undefined) {
			const msbKey = _chKey(lsbBinding.source.ch, lsbBinding.source.id);
			const msb = device.msbValues.get(msbKey);
			if (msb === undefined) {
				_pushLearnLog({ ...log, mapped: false, note: 'pitch LSB arrived before any MSB - dropped pair' });
				return;
			}
			const raw = combine14(msb, d2);
			const value01raw = raw / 16383;
			const value: MidiInputValue = {
				kind: 'continuous14',
				value01: lsbBinding.invert === true ? 1 - value01raw : value01raw,
				raw
			};
			_emit(device, lsbBinding, value, log);
			return;
		}
	}
	// Binding lookup: shifted layer first while held, unshifted fallback.
	const binding =
		(midiState.shiftHeld ? device.index.get(_bindingKey(true, src)) : undefined) ??
		device.index.get(_bindingKey(false, src));
	if (binding === undefined) {
		_pushLearnLog({ ...log, mapped: false, note: 'unmapped source' });
		return;
	}
	// 14-bit MSB leg: store and wait for the LSB to complete the pair.
	if (
		binding.action.type === 'deck_pitch' &&
		binding.action.lsbOffset !== null &&
		src.kind === 'cc'
	) {
		device.msbValues.set(_chKey(src.ch, src.id), d2);
		_pushLearnLog({
			...log,
			mapped: true,
			note: 'deck_pitch MSB stored (awaiting LSB)',
			action: binding.action
		});
		return;
	}
	if (src.kind === 'pitchbend') {
		const raw = combine14(d2, d1); // pitchbend wire order: d1 = LSB, d2 = MSB
		const value01raw = raw / 16383;
		_emit(
			device,
			binding,
			{
				kind: 'continuous14',
				value01: binding.invert === true ? 1 - value01raw : value01raw,
				raw
			},
			log
		);
		return;
	}
	_emit(device, binding, _valueFor(binding, src, status, d2), log);
}

// ------------------------------------------------------------- public API

/** Drop every cached resolution and rescan, so a registry change reaches the
 * ports that are already plugged in. No-op before initMidi(). */
function _reresolvePorts(): void {
	if (_access === null) return;
	for (const dev of _resolved.values()) dev.input.onmidimessage = null;
	_resolved.clear();
	_rescanPorts();
}

/** Validate a map's static shape WITHOUT registering it: the same fail-fast
 * checks registerDeviceMap performs below, minus the registry-clash check
 * (which depends on what is already registered). Exported so a caller
 * committing a whole batch (loadInstalledDeviceMaps()) can prove every map
 * in the batch is registrable BEFORE mutating the registry - the daemon's
 * nameMatch deny list (routes/midi_maps.py) cannot invoke the browser's own
 * regex engine, so it is necessarily partial, and a document it accepted can
 * still fail here. */
export function validateDeviceMap(map: DeviceMap): void {
	if (map.nameMatch.length === 0) {
		throw new Error(`validateDeviceMap(${map.vendor}): nameMatch must be non-empty`);
	}
	new RegExp(map.nameMatch); // fail fast on an invalid pattern
	_buildIndex(map); // fail fast on duplicate/invalid bindings
}

/** Register a device map. Call for each supported controller BEFORE
 * initMidi(); registering after init re-resolves connected devices.
 *
 * tier defaults to 'builtin' (a map compiled into the bundle). Maps onboarded
 * at runtime register as 'installed' and shadow their builtin twin. Two maps
 * on the same nameMatch in the SAME tier is a wiring bug, not a shadow, so it
 * throws. */
export function registerDeviceMap(map: DeviceMap, tier: DeviceMapTier = 'builtin'): void {
	validateDeviceMap(map);
	const clash = _deviceMaps.find((e) => e.tier === tier && e.map.nameMatch === map.nameMatch);
	if (clash !== undefined) {
		throw new Error(
			`registerDeviceMap(${map.vendor}): ${tier} map for '${map.nameMatch}' is already registered (${clash.map.vendor}); unregister it first`
		);
	}
	_deviceMaps.push({ map, tier });
	_reresolvePorts();
}

/** Remove one registered map. Returns false when nothing matched, so an
 * uninstall that hit nothing is visible to the caller rather than a silent
 * no-op. Removing an 'installed' map reveals the 'builtin' it shadowed. */
export function unregisterDeviceMap(nameMatch: string, tier: DeviceMapTier): boolean {
	const at = _deviceMaps.findIndex((e) => e.tier === tier && e.map.nameMatch === nameMatch);
	if (at === -1) return false;
	_deviceMaps.splice(at, 1);
	_reresolvePorts();
	return true;
}

/** Every registered map with its tier, for the UI to name what shadows what. */
export function listDeviceMaps(): readonly RegisteredDeviceMap[] {
	return [..._deviceMaps];
}

/** Which map a given WebMIDI port name would resolve to, tier precedence
 * applied. Exposed for the onboarding UI, which has to answer 'is this
 * controller already known' before a device is even connected. */
export function resolveMapForPort(portName: string): DeviceMap | null {
	return _resolveMap(portName);
}

/** Register THE action handler (the glue layer). Exactly one; a second
 * registration is a wiring bug and throws. */
export function registerActionHandler(
	handler: (action: MidiAction, value: MidiInputValue, deviceId: string, pressT0Ms: number) => void
): void {
	if (_actionHandler !== null) {
		throw new Error('registerActionHandler: a handler is already registered');
	}
	_actionHandler = handler;
}

/** Release THE action handler. The glue's teardown calls this: without it a
 * detach left the handler registered, so re-attaching after a /performance
 * remount threw 'a handler is already registered'. */
export function unregisterActionHandler(): void {
	_actionHandler = null;
}

/** Request WebMIDI access (sysex: false - spike 2a: neither controller
 * needs SysEx) and start dispatching. Throws on unsupported browsers and
 * on user denial; permission state is mirrored in midiState. */
export async function initMidi(): Promise<void> {
	if (!webMidiSupported()) {
		midiState.permission = 'unsupported';
		throw new Error('WebMIDI is not supported in this browser (Safari has no WebMIDI - use Chrome/Edge)');
	}
	midiState.permission = 'prompt';
	try {
		_access = await navigator.requestMIDIAccess({ sysex: false });
	} catch (exc) {
		midiState.permission = 'denied';
		throw new Error(`WebMIDI permission denied: ${String(exc)}`);
	}
	midiState.permission = 'granted';
	_access.onstatechange = () => _rescanPorts();
	_rescanPorts();
}

/** Queue an LED write (Note On, velocity = colour/state - spike 2a: FLX10
 * pad RGB is Note-On velocity 1..127; Mixtour LEDs are plain Note On/Off).
 * Coalesced per (ch,note) and flushed every LED_THROTTLE_MS. */
export function sendLed(deviceId: string, ch: number, note: number, velocity: number): void {
	if (ch < 1 || ch > 16) throw new RangeError(`sendLed: ch must be 1..16, got ${ch}`);
	if (note < 0 || note > 127) throw new RangeError(`sendLed: note must be 0..127, got ${note}`);
	if (velocity < 0 || velocity > 127) {
		throw new RangeError(`sendLed: velocity must be 0..127, got ${velocity}`);
	}
	const device = _resolved.get(deviceId);
	if (device === undefined) {
		throw new Error(`sendLed: unknown device id ${deviceId}`);
	}
	if (device.output === null) {
		throw new Error(`sendLed: device ${device.input.name} has no MIDI output port`);
	}
	let queue = _ledQueues.get(deviceId);
	if (queue === undefined) {
		queue = new Map();
		_ledQueues.set(deviceId, queue);
	}
	queue.set(`${MIDI_STATUS_NOTE_ON}:${_chKey(ch, note)}`, velocity);
	if (_ledTimer === null) {
		_ledTimer = setInterval(_flushLedQueues, LED_THROTTLE_MS);
	}
}

/** Queue a Control Change write on the SAME coalescing queue as sendLed.
 *
 * Needed for hardware whose level meters are host-driven rather than
 * self-metering: the DDJ-400's channel VU is a CC stream (Bn 02 hh, [PDF]
 * p.2 M12), not a Note On, so it cannot ride the LedRule path. Coalescing
 * matters MORE here than for LEDs - a meter pump emits continuously, and
 * without it a 30 Hz pump on two channels would flood the port.
 *
 * Same fail-fast contract as sendLed: an out-of-range argument or a device
 * with no output port throws rather than silently dropping the write. */
export function sendCc(deviceId: string, ch: number, cc: number, value: number): void {
	if (ch < 1 || ch > 16) throw new RangeError(`sendCc: ch must be 1..16, got ${ch}`);
	if (cc < 0 || cc > 127) throw new RangeError(`sendCc: cc must be 0..127, got ${cc}`);
	if (value < 0 || value > 127) {
		throw new RangeError(`sendCc: value must be 0..127, got ${value}`);
	}
	const device = _resolved.get(deviceId);
	if (device === undefined) {
		throw new Error(`sendCc: unknown device id ${deviceId}`);
	}
	if (device.output === null) {
		throw new Error(`sendCc: device ${device.input.name} has no MIDI output port`);
	}
	let queue = _ledQueues.get(deviceId);
	if (queue === undefined) {
		queue = new Map();
		_ledQueues.set(deviceId, queue);
	}
	queue.set(`${MIDI_STATUS_CC}:${_chKey(ch, cc)}`, value);
	if (_ledTimer === null) {
		_ledTimer = setInterval(_flushLedQueues, LED_THROTTLE_MS);
	}
}

function _flushLedQueues(): void {
	let any = false;
	for (const [deviceId, queue] of _ledQueues) {
		const device = _resolved.get(deviceId);
		if (device === undefined || device.output === null) {
			// Device unplugged since queueing: drop ITS queue loudly, keep others.
			console.error(`[midi] dropping ${queue.size} queued LED writes for vanished device ${deviceId}`);
			_ledQueues.delete(deviceId);
			continue;
		}
		for (const [key, value] of queue) {
			const [statusStr, chStr, idStr] = key.split(':');
			const status = Number(statusStr);
			const ch = Number(chStr);
			const id = Number(idStr);
			if (status !== MIDI_STATUS_NOTE_ON && status !== MIDI_STATUS_CC) {
				throw new Error(`_flushLedQueues: unknown queued status 0x${status.toString(16)}`);
			}
			device.output.send([status | (ch - 1), id, value]);
			any = true;
		}
		queue.clear();
	}
	if (!any && _ledTimer !== null) {
		clearInterval(_ledTimer);
		_ledTimer = null;
	}
}

/** The resolved DeviceMap for a connected device (glue needs its LedRules). */
export function getDeviceMap(deviceId: string): DeviceMap | null {
	return _resolved.get(deviceId)?.map ?? null;
}

/** TEST-ONLY: is a handler currently registered? Lets the glue's teardown
 * be asserted on the state webmidi actually holds, rather than only on the
 * absence of a throw from a later attach. */
export function _actionHandlerRegisteredForTests(): boolean {
	return _actionHandler !== null;
}

/** TEST-ONLY: reset all module state between unit tests. */
export function _resetMidiForTests(): void {
	for (const dev of _resolved.values()) dev.input.onmidimessage = null;
	_resolved.clear();
	_deviceMaps.length = 0;
	_ledQueues.clear();
	if (_ledTimer !== null) {
		clearInterval(_ledTimer);
		_ledTimer = null;
	}
	_actionHandler = null;
	_access = null;
	midiState.permission = 'prompt';
	midiState.devices = [];
	midiState.shiftHeld = false;
	learnLog.length = 0;
}
