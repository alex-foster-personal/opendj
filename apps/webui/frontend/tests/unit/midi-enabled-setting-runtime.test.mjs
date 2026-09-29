/**
 * requirement: CHROME-07
 *
 * The rb.midi_enabled setting acts on the live MIDI runtime, not only on a
 * stored flag (Codex P1 on PR #3896), and a request superseded by turning
 * MIDI off neither attaches nor persists (Codex P2 on 5c6a110fd).
 *
 * NO FAKES (AGENTS.md "No mocks and locked real fixtures"). Nothing here
 * replaces navigator.requestMIDIAccess, a MIDI port, localStorage or any
 * module. The behavioral half runs the real settings entry point in a child
 * Node process started with Node's own Web Storage, where WebMIDI genuinely
 * does not exist, and asserts what the real code reports about that.
 *
 * UNAVAILABLE: runtime acceptance against a REAL WebMIDI grant. Probed on
 * Tue 29 Sep 2026 in this container with the repo's Playwright 1.61.1 and
 * its Chromium 1194 (headless), on a secure http://127.0.0.1 origin:
 *   - grantPermissions(['midi']) -> requestMIDIAccess rejects NotAllowedError
 *   - grantPermissions(['midi', 'midi-sysex']) -> rejects InvalidStateError
 *     "Platform dependent initialization failed"
 * Chromium's Linux MIDI backend needs the ALSA sequencer, and this host has
 * no /dev/snd or /proc/asound. A real-Chromium spec could therefore only
 * reach the same failure branch this file already covers. Not verified on
 * CI runners. So the grant-side paths (inputs attached on enable, released
 * on disable, a late grant dropped when superseded) are pinned here by
 * reading the source, and their runtime proof needs a host with a MIDI
 * backend (a real controller, or snd-virmidi loaded).
 *
 * Regression lines:
 * - if enabling from settings never reaches the access request then the
 *   toggle reads "on" while no controller works until a reload
 * - if a failed (unsupported) request leaves the action glue attached or
 *   the choice persisted then the app claims MIDI it does not have
 * - if a grant that resolves after MIDI was turned off still attaches or
 *   persists then the controller comes back on against the user's choice
 * - if disabling rewrites midiState.permission then the panel lies about
 *   what the browser granted
 */
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

const FRONTEND_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const read = (rel) => fs.readFileSync(path.join(FRONTEND_ROOT, rel), 'utf8');

let results;

before(() => {
	const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'midi-setting-runtime-'));
	try {
		const child = spawnSync(
			process.execPath,
			[
				'--experimental-webstorage',
				`--localstorage-file=${path.join(dir, 'localstorage.db')}`,
				// new URL(..., import.meta.url) is also how knip sees a helper that
				// is spawned rather than imported (prefs-golden-probe.mjs precedent).
				fileURLToPath(new URL('./midi-enabled-setting-runtime.child.mjs', import.meta.url))
			],
			{ cwd: FRONTEND_ROOT, encoding: 'utf8', timeout: 120_000 }
		);
		const line = (child.stdout ?? '').split('\n').find((l) => l.startsWith('RESULT '));
		if (child.status !== 0 || line === undefined) {
			throw new Error(
				`child run failed (status ${child.status}, signal ${child.signal}):\n${child.stderr}\n${child.stdout}`
			);
		}
		results = JSON.parse(line.slice('RESULT '.length));
	} finally {
		fs.rmSync(dir, { recursive: true, force: true });
	}
});

test('the child really ran without WebMIDI, against real storage (instrument check)', () => {
	for (const [name, snap] of Object.entries(results)) {
		if (name === 'displayReactivity') continue; // a render trace, not a snapshot
		assert.equal(snap.navigatorHasWebMidi, false, `${name}: this file asserts the no-WebMIDI branch`);
	}
	// Real storage both ways: an opt-in written was read back before the request cleared it.
	assert.equal(results.autoEnableOptedIn.persistedBeforeAuto, true);
	assert.equal(results.enableFromSettings.settingRightAfterEnable, true);
});

test('page-load auto-enable stays idle without an opt-in (control)', () => {
	const s = results.idleAutoEnable;
	assert.equal(s.lastError, null);
	assert.equal(s.permission, 'prompt', 'the request path was never entered');
	assert.equal(s.glueAttached, false);
});

test('enabling rb.midi_enabled from settings reaches the real access request, which reports WebMIDI unsupported', () => {
	const s = results.enableFromSettings;
	assert.equal(s.permission, 'unsupported');
	assert.match(s.lastError, /not supported/i);
	assert.equal(s.requestPending, false);
});

test('an unsupported enable neither attaches the glue nor persists success', () => {
	const s = results.enableFromSettings;
	assert.equal(s.glueAttached, false, 'no access, nothing for the glue to drive');
	assert.equal(s.persisted, false);
	assert.equal(s.setting, false, 'the settings read-back must not claim MIDI is on');
	assert.equal(s.devices, 0);
});

test('turning MIDI off while its request is pending ends fully off', () => {
	const s = results.disableWhilePending;
	assert.equal(s.pendingAtDisable, true, 'the disable must land inside the pending window to mean anything');
	assert.equal(s.requestPending, false);
	assert.equal(s.glueAttached, false);
	assert.equal(s.persisted, false);
	assert.equal(s.setting, false);
});

test('off and on again while a request is pending settles without a stuck request', () => {
	const s = results.reenableWhilePending;
	assert.equal(s.pendingAtToggle, true);
	assert.equal(s.requestPending, false);
	// Here the fresh request also meets no WebMIDI, so it ends off and says why.
	assert.match(s.lastError, /not supported/i);
	assert.equal(s.glueAttached, false);
	assert.equal(s.persisted, false);
});

test('page-load auto-enable still requests access for a persisted opt-in (control)', () => {
	const s = results.autoEnableOptedIn;
	assert.equal(s.permission, 'unsupported', 'the request path ran');
	assert.match(s.lastError, /not supported/i);
	assert.equal(s.persisted, false, 'a failed request forgets the opt-in');
});

test('disabling from settings leaves the permission state as the browser reported it', () => {
	const s = results.disableFromSettings;
	assert.equal(s.settingRightAfterDisable, false);
	assert.equal(s.permission, 'unsupported', 'disable must not rewrite permission');
	assert.equal(s.glueAttached, false);
	assert.equal(s.persisted, false);
});

test('the settings MIDI toggle re-renders on toggle, on a denied request clearing it, and on a disk hydrate', () => {
	// Initial read, the optimistic toggle, the settle that clears it, the
	// disk-backed choice: four distinct renders, not one frozen value.
	assert.deepEqual(results.displayReactivity.seen, [false, true, false, true]);
});

test('every write of the choice goes through persistMidiEnabled, which signals the display', () => {
	const choiceSrc = read('src/lib/components/rb/midi/midi-enabled-choice.ts');
	const persist = body(choiceSrc, 'export function persistMidiEnabled(enabled: boolean)');
	assert.match(persist, /bumpMidiEnabledTick\(\);/);
	assert.match(body(choiceSrc, 'export function midiEnabledPersisted()'), /void midiEnabledTick\.n;/);
	// setMidiEnabledChoice (grant, denial, superseded settle) writes through it.
	assert.match(body(uiStateSrc, 'export function setMidiEnabledChoice(enabled: boolean)'), /persistMidiEnabled\(enabled\);/);
	assert.match(body(choiceSrc, 'export function hydrateMidiEnabledFromDisk('), /persistMidiEnabled\(body\.midi_enabled\);/);
});

// ---------------------------------------------------------------- source
// The grant-side paths need a real WebMIDI grant (UNAVAILABLE, see header),
// so their structure is pinned here instead.

const uiStateSrc = read('src/lib/components/rb/midi/midi-ui-state.svelte.ts');
const webmidiSrc = read('src/lib/rb/midi/webmidi.svelte.ts');
const applySrc = read('src/lib/settings/apply.ts');

function body(src, signature) {
	const start = src.indexOf(signature);
	assert.ok(start >= 0, `missing ${signature}`);
	return src.slice(start, src.indexOf('\n}\n', start));
}

test('a grant that resolves after MIDI was turned off is released before any attach or persist', () => {
	const req = body(uiStateSrc, 'async function _requestMidiAccess(generation: number)');
	const initAt = req.indexOf('await initMidi();');
	const checkAt = req.indexOf('if (generation !== _requestGeneration) {', initAt);
	const attachAt = req.indexOf('_detachMidiGlue = attachMidiGlue();');
	const persistAt = req.indexOf('setMidiEnabledChoice(true);');
	assert.ok(initAt >= 0 && checkAt > initAt, 'the generation check must follow initMidi()');
	assert.ok(attachAt > checkAt, 'the glue attaches only after the check');
	assert.ok(persistAt > checkAt, 'the choice persists only after the check');
	const superseded = req.slice(checkAt, req.indexOf('}', checkAt));
	assert.match(superseded, /releaseMidiInputs\(\);\s*return;/);
	assert.match(req, /if \(generation === _requestGeneration\) setMidiEnabledChoice\(false\);/,
		'a superseded failure must not clobber the choice made since');
});

test('each request takes a new generation and disableMidi supersedes it before tearing down', () => {
	assert.match(body(uiStateSrc, 'export async function requestMidiAccess()'), /const generation = \+\+_requestGeneration;/);
	const disable = body(uiStateSrc, 'export function disableMidi()');
	assert.match(disable, /_requestGeneration \+= 1;\s*detachMidiGlueForRouteUnmount\(\);\s*releaseMidiInputs\(\);/);
});

test('enable while a superseded request is pending waits for it, then requests afresh', () => {
	const applyFn = body(uiStateSrc, 'export async function applyMidiEnabledSetting(enabled: boolean)');
	assert.match(applyFn, /if \(pending\.generation === _requestGeneration\) return;/);
	assert.match(applyFn, /await pending\.done;/);
	assert.match(applyFn, /setMidiEnabledChoice\(true\);\s*await maybeAutoEnableMidi\(\);/);
});

test('releaseMidiInputs detaches every input and hot-plug listener and never writes permission', () => {
	const release = body(webmidiSrc, 'export function releaseMidiInputs()');
	assert.match(release, /_access\.onstatechange = null;/);
	assert.match(release, /for \(const dev of _resolved\.values\(\)\) dev\.input\.onmidimessage = null;/);
	assert.doesNotMatch(release, /midiState\.permission\s*=/);
});

test('settings/apply.ts reaches the MIDI runtime only through a dynamic import', () => {
	assert.doesNotMatch(applySrc, /import\s*\{[^}]*\}\s*from\s*'\$lib\/components\/rb\/midi\/midi-ui-state\.svelte'/);
	assert.match(applySrc, /await import\('\$lib\/components\/rb\/midi\/midi-ui-state\.svelte'\)/);
	assert.match(applySrc, /await applyMidiEnabledSetting\(enabled\);/);
});
