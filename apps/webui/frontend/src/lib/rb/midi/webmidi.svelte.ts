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
import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import {
	bindingKey as _bindingKey,
	chKey as _chKey,
	combine14,
	decodeRelative,
	decodeSource
} from '$lib/rb/midi/decode';
import { rearmMidiTakeoverDevice } from '$lib/rb/midi/takeover-state.svelte';
import { djioRedirectTarget } from '$lib/rb/audio-output-topology';

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

/** Reactive WebMIDI surface state. */
export const midiState: {
	permission: MidiPermission;
	devices: MidiDeviceInfo[];
	shiftHeld: boolean;
} = $state({
	permission: 'prompt',
	devices: [],
	shiftHeld: false
});

/** Rolling learn log, newest first. THE debugging tool for writing device
 * maps: unmapped traffic is captured here, never dropped. */
export const learnLog: LearnLogEntry[] = $state([]);

// -------------------------------------------------- non-reactive runtime

interface _MidiOutputPort {
	send(data: number[]): void;
}

interface _ResolvedDevice {
	id: string;
	name: string;
	manufacturer: string;
	detachInput: () => void;
	output: _MidiOutputPort | null;
	map: DeviceMap | null;
	/** binding lookup key -> binding (see _bindingKey). */
	index: Map<string, MidiBinding>;
	/** 14-bit pairing state per MSB controller: `${ch}:${msbId}` -> value. */
	msbValues: Map<string, number>;
	/** LSB controller reverse index: `${ch}:${lsbId}` -> MSB binding. */
	lsbIndex: Map<string, MidiBinding>;
}

let _access: MIDIAccess | null = null;
let _transport: 'none' | 'webmidi' | 'native' = 'none';
/** Tauri 2's unlisten resolves once the `plugin:event|unlisten` IPC has landed. */
let _nativeUnlisten: (() => void | Promise<void>) | null = null;
let _nativePollTimer: ReturnType<typeof setInterval> | null = null;
let _djioRedirectIssued = false;

interface _NativeMidiDevice {
	id: string;
	name: string;
	manufacturer: string;
	hasOutput: boolean;
}

interface _NativeMidiMessage {
	deviceId: string;
	deviceName: string;
	timestampMicros: number;
	data: number[];
}
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
/** Synchronous dispatch context. The public handler signature remains the
 * receipt-stamp contract; action glue reads this only during that call. */
let _activeControlId: string | undefined;

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

function _findOutputFor(access: MIDIAccess, inputName: string): _MidiOutputPort | null {
	for (const output of access.outputs.values()) {
		if (output.name === inputName) return output;
	}
	return null;
}

function _publishResolvedDevices(): void {
	midiState.devices = [..._resolved.values()].map((d) => ({
		id: d.id,
		name: d.name,
		manufacturer: d.manufacturer,
		mapVendor: d.map?.vendor ?? null,
		hasOutput: d.output !== null
	}));
}

function _rescanWebMidiPorts(): void {
	if (_access === null) throw new Error('_rescanWebMidiPorts before WebMIDI init');
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
			id: input.id,
			name,
			manufacturer: input.manufacturer ?? '',
			detachInput: () => {
				input.onmidimessage = null;
			},
			output: _findOutputFor(_access, name),
			map,
			index,
			msbValues: new Map(),
			lsbIndex
		};
		input.onmidimessage = (ev: MIDIMessageEvent) => _dispatch(device, ev);
		_resolved.set(input.id, device);
		rearmMidiTakeoverDevice(input.id);
	}
	for (const id of [..._resolved.keys()]) {
		if (!seen.has(id)) {
			const dev = _resolved.get(id);
			if (dev !== undefined) dev.detachInput();
			_resolved.delete(id);
			_ledQueues.delete(id);
			rearmMidiTakeoverDevice(id);
		}
	}
	_publishResolvedDevices();
}

function _nativeOutput(deviceId: string): _MidiOutputPort {
	return {
		send(data: number[]): void {
			void invoke('native_midi_send', { deviceId, data }).catch((exc: unknown) => {
				console.error(`[native-midi] output failed for ${deviceId}`, exc);
			});
		}
	};
}

/** Resolve a native snapshot; returns the djio URL this page must leave for, or null to stay. */
function _applyNativeSnapshot(snapshot: _NativeMidiDevice[]): string | null {
	const seen = new Set(snapshot.map((device) => device.id));
	for (const found of snapshot) {
		const existing = _resolved.get(found.id);
		if (existing !== undefined) {
			existing.name = found.name;
			existing.manufacturer = found.manufacturer;
			existing.output = found.hasOutput ? _nativeOutput(found.id) : null;
			continue;
		}
		const map = _resolveMap(found.name);
		const { index, lsbIndex } =
			map !== null
				? _buildIndex(map)
				: { index: new Map<string, MidiBinding>(), lsbIndex: new Map<string, MidiBinding>() };
		_resolved.set(found.id, {
			id: found.id,
			name: found.name,
			manufacturer: found.manufacturer,
			detachInput: () => {},
			output: found.hasOutput ? _nativeOutput(found.id) : null,
			map,
			index,
			msbValues: new Map(),
			lsbIndex
		});
		rearmMidiTakeoverDevice(found.id);
	}
	for (const id of [..._resolved.keys()]) {
		if (seen.has(id)) continue;
		_resolved.get(id)?.detachInput();
		_resolved.delete(id);
		_ledQueues.delete(id);
		rearmMidiTakeoverDevice(id);
	}
	_publishResolvedDevices();
	const profiles = new Set(
		[..._resolved.values()].flatMap((device) =>
			device.map?.nativeAudioProfile === undefined ? [] : [device.map.nativeAudioProfile]
		)
	);
	if (profiles.size > 1) {
		console.error('[native-midi] connected controller maps request conflicting audio profiles', [
			...profiles
		]);
	} else if (profiles.size === 1) {
		// IOPIN-12: a page already on djio (a stereo-fallback page keeps its
		// param) or on extroute returns null, so a rescan can never loop the reload.
		return djioRedirectTarget(window.location.href, [...profiles][0]);
	}
	return null;
}

/** True when this page is leaving for djio. Every snapshot until navigation lands
 * answers that, so a later initMidi() never subscribes here, and the flag keeps
 * replace() to one call per page. */
async function _rescanNativePorts(): Promise<boolean> {
	const snapshot = await invoke<_NativeMidiDevice[]>('native_midi_snapshot');
	const target = _applyNativeSnapshot(snapshot);
	if (target === null) return false;
	if (!_djioRedirectIssued) {
		_djioRedirectIssued = true;
		await _releaseNativeMidiThenNavigate(target);
	}
	return true;
}

/** IOPIN-12: a page that subscribed before the Mixtour appeared (a hot-plug, or an
 * in-app navigation that dropped djio) must not reload with its listener live: Tauri
 * keeps it registered across the reload, so every press would arrive twice. The
 * unlisten IPC is awaited because a navigation can cancel one still in flight. */
async function _releaseNativeMidiThenNavigate(target: string): Promise<void> {
	if (_nativePollTimer !== null) {
		clearInterval(_nativePollTimer);
		_nativePollTimer = null;
	}
	const unlisten = _nativeUnlisten;
	_nativeUnlisten = null;
	_transport = 'none';
	try {
		await unlisten?.();
	} finally {
		window.location.replace(target);
	}
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
		const decoded = decodeRelative(d2);
		const unit = binding.relativeUnit === true && decoded !== 0 ? Math.sign(decoded) : decoded;
		return { kind: 'relative', delta: binding.invert === true ? -unit : unit };
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
		// Layer changes can rebind an identical physical CC to a different
		// scalar. Require a fresh pickup rather than carrying its old position
		// into that layer.
		rearmMidiTakeoverDevice(device.id);
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
		_activeControlId = _bindingKey(binding.shift === true, binding.source);
		try {
			_actionHandler(binding.action, value, device.id, log.ts);
		} finally {
			_activeControlId = undefined;
		}
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

function _dispatch(device: _ResolvedDevice, ev: { data: Uint8Array | null }): void {
	const data = ev.data;
	if (data === null || data.length === 0) return;
	const status = data[0];
	if (status >= 0xf8) return; // realtime clock ticks: pure noise, not logged
	const d1 = data.length > 1 ? data[1] : 0;
	const d2 = data.length > 2 ? data[2] : 0;
	const src = decodeSource(status, d1);
	const log: Omit<LearnLogEntry, 'mapped' | 'note'> = {
		ts: performance.now(),
		deviceId: device.id,
		deviceName: device.name,
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
	if (_transport === 'webmidi') {
		for (const dev of _resolved.values()) dev.detachInput();
		_resolved.clear();
		_rescanWebMidiPorts();
		return;
	}
	if (_transport === 'native') {
		for (const device of _resolved.values()) {
			const map = _resolveMap(device.name);
			const { index, lsbIndex } =
				map !== null
					? _buildIndex(map)
					: { index: new Map<string, MidiBinding>(), lsbIndex: new Map<string, MidiBinding>() };
			device.map = map;
			device.index = index;
			device.lsbIndex = lsbIndex;
			device.msbValues.clear();
		}
		_publishResolvedDevices();
	}
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

/** The physical source of the action currently being synchronously handled.
 * It is intentionally undefined for programmatic action-glue calls. */
export function activeMidiControlId(): string | undefined {
	return _activeControlId;
}

/** Release THE action handler. The glue's teardown calls this: without it a
 * detach left the handler registered, so re-attaching after a /performance
 * remount threw 'a handler is already registered'. */
export function unregisterActionHandler(): void {
	_actionHandler = null;
	_activeControlId = undefined;
}

function _hasNativeMidiBridge(): boolean {
	return (
		typeof window !== 'undefined' &&
		typeof (window as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ === 'object'
	);
}

/** Request WebMIDI access (sysex: false - spike 2a: neither controller
 * needs SysEx) and start dispatching. In the installed macOS shell, where
 * WKWebView has no WebMIDI, use the shell's transport-only CoreMIDI bridge.
 * Mapping and action dispatch remain on this one shared code path. */
export async function initMidi(): Promise<void> {
	// The persisted local choice and the daemon-backed preference can hydrate
	// separately. Both intentionally call the same request path, so make the
	// transport initialization itself idempotent: a second request rescans but
	// must not register a second event listener or hot-plug timer.
	if (_transport === 'webmidi') {
		_rescanWebMidiPorts();
		return;
	}
	if (_transport === 'native') {
		await _rescanNativePorts();
		return;
	}
	if (typeof navigator !== 'undefined' && navigator.requestMIDIAccess !== undefined) {
		midiState.permission = 'prompt';
		try {
			_access = await navigator.requestMIDIAccess({ sysex: false });
		} catch (exc) {
			midiState.permission = 'denied';
			throw new Error(`WebMIDI permission denied: ${String(exc)}`);
		}
		_transport = 'webmidi';
		midiState.permission = 'granted';
		_access.onstatechange = () => _rescanWebMidiPorts();
		_rescanWebMidiPorts();
		return;
	}
	if (!_hasNativeMidiBridge()) {
		midiState.permission = 'unsupported';
		throw new Error(
			'MIDI is not supported here (use Chrome/Edge, or the Open DJ macOS app with its native bridge)'
		);
	}
	midiState.permission = 'prompt';
	try {
		// Discover before subscribing. The first native snapshot may add the
		// controller's required audio profile and navigate this page. Registering
		// a Tauri event listener before that navigation leaves the old webview
		// callback alive and every physical message arrives twice after reload.
		if (await _rescanNativePorts()) return;
		_nativeUnlisten = await listen<_NativeMidiMessage>(
			'opendj-native-midi-message',
			(event) => {
				const device = _resolved.get(event.payload.deviceId);
				if (device === undefined) {
					console.error(
						`[native-midi] input arrived for unknown device ${event.payload.deviceId}; rescanning`
					);
					void _rescanNativePorts();
					return;
				}
				_dispatch(device, { data: new Uint8Array(event.payload.data) });
			}
		);
	} catch (exc) {
		midiState.permission = 'denied';
		_nativeUnlisten?.();
		_nativeUnlisten = null;
		throw new Error(`native MIDI bridge failed: ${String(exc)}`);
	}
	_transport = 'native';
	midiState.permission = 'granted';
	_nativePollTimer = setInterval(() => {
		void _rescanNativePorts().catch((exc: unknown) => {
			console.error('[native-midi] hot-plug rescan failed', exc);
		});
	}, 1000);
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
		throw new Error(`sendLed: device ${device.name} has no MIDI output port`);
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
		throw new Error(`sendCc: device ${device.name} has no MIDI output port`);
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
	for (const dev of _resolved.values()) dev.detachInput();
	_resolved.clear();
	_deviceMaps.length = 0;
	_ledQueues.clear();
	if (_ledTimer !== null) {
		clearInterval(_ledTimer);
		_ledTimer = null;
	}
	_actionHandler = null;
	_nativeUnlisten?.();
	_nativeUnlisten = null;
	if (_nativePollTimer !== null) {
		clearInterval(_nativePollTimer);
		_nativePollTimer = null;
	}
	_access = null;
	_transport = 'none';
	_djioRedirectIssued = false;
	midiState.permission = 'prompt';
	midiState.devices = [];
	midiState.shiftHeld = false;
	learnLog.length = 0;
}
