/**
 * Native-shell transport wiring guard for the Mixtour Pro.
 *
 * These source assertions are structural regression guards, not hardware
 * acceptance. The dated Air report remains the evidence for real CoreMIDI and
 * four-channel audio behavior.
 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const root = new URL('../../', import.meta.url);
const read = (path) => readFile(new URL(path, root), 'utf8');

test('Mixtour Pro requests the native master 1/2 plus cue 3/4 topology', async () => {
	const [map, runtime] = await Promise.all([
		read('src/lib/rb/midi/maps/reloop-mixtour-pro.ts'),
		read('src/lib/rb/midi/webmidi.svelte.ts')
	]);
	assert.match(map, /nativeAudioProfile:\s*'master12-cue34'/);
	assert.match(runtime, /next\.searchParams\.set\('djio', \[\.\.\.profiles\]\[0\]\)/);
});

test('installed-shell MIDI is a bounded CoreMIDI transport into the shared runtime', async () => {
	const [native, runtime] = await Promise.all([
		read('../../desktop/src-tauri/src/midi.rs'),
		read('src/lib/rb/midi/webmidi.svelte.ts')
	]);
	assert.match(native, /MidiInputConnection/);
	assert.match(native, /data\.len\(\) != 3/);
	assert.match(native, /opendj-native-midi-message/);
	assert.match(runtime, /listen<_NativeMidiMessage>/);
	assert.match(runtime, /_dispatch\(device, \{ data: new Uint8Array\(event\.payload\.data\) \}\)/);
	assert.match(runtime, /if \(await _rescanNativePorts\(\)\) return;\s*_nativeUnlisten = await listen/);
	assert.match(runtime, /if \(_transport === 'native'\) \{\s*await _rescanNativePorts\(\);\s*return;/);
});

test('remote shell authority exposes only the bounded MIDI commands and event listener', async () => {
	const [buildScript, capabilityText] = await Promise.all([
		read('../../desktop/src-tauri/build.rs'),
		read('../../desktop/src-tauri/capabilities/midi.json')
	]);
	const capability = JSON.parse(capabilityText);
	assert.match(buildScript, /commands\(&\["native_midi_snapshot", "native_midi_send"\]\)/);
	assert.deepEqual(capability.permissions, [
		'allow-native-midi-snapshot',
		'allow-native-midi-send',
		'core:event:allow-listen',
		'core:event:allow-unlisten'
	]);
});

test('four-channel graph assigns stereo master to 1/2 and stereo monitor to 3/4', async () => {
	const engine = await read('src/lib/rb/audio-engine.svelte.ts');
	assert.match(engine, /masterSplitter\.connect\(merger, 0, 0\)/);
	assert.match(engine, /masterSplitter\.connect\(merger, 1, 1\)/);
	assert.match(engine, /cueSplitter\.connect\(merger, 0, 2\)/);
	assert.match(engine, /cueSplitter\.connect\(merger, 1, 3\)/);
	assert.match(engine, /setMultichannelMonitorActive\(true\)/);
});
