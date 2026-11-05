import * as assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { test } from 'node:test';

import { OUTPUT_PROBE_HELPER, coreAudioOutputDevices, outputHealthJson, reconnectSpeakerJson } from '../src/output-health';
import { POSIX_ONLY, scratchDir } from './helpers';

function payloadWithHelper(script: string): string {
	const payload = path.join(scratchDir('probe'), 'payload');
	const helper = path.join(payload, OUTPUT_PROBE_HELPER);
	fs.mkdirSync(path.dirname(helper), { recursive: true });
	fs.writeFileSync(helper, `#!/bin/sh\n${script}\n`, { mode: 0o755 });
	return payload;
}

test('off macOS the probe is an honest unknown, as the Tauri shell answered', () => {
	const body = JSON.parse(outputHealthJson('probe', null, 'linux')) as Record<string, unknown>;
	assert.equal(body.verdict, 'unknown');
	assert.equal(body.probe_available, false);
	assert.equal(body.reason, 'installed macOS shell required for OS output probe');
	const cycle = JSON.parse(outputHealthJson('switch', null, 'linux')) as Record<string, unknown>;
	assert.deepEqual(cycle, { cycled: false, error: 'installed macOS shell required for output device cycling' });
});

test('on macOS without the helper the verdict is unknown and says why', () => {
	const body = JSON.parse(outputHealthJson('probe', scratchDir('no-helper'), 'darwin')) as Record<string, unknown>;
	assert.equal(body.verdict, 'unknown');
	assert.match(String(body.reason), /helper not bundled/);
});

test('on macOS the helper answers, with the action passed through', { skip: POSIX_ONLY('a #!/bin/sh helper script') }, () => {
	const payload = payloadWithHelper('printf \'{"verdict":"ok","action":"%s"}\' "$1"');
	assert.deepEqual(JSON.parse(outputHealthJson('probe', payload, 'darwin')), { verdict: 'ok', action: 'probe' });
	assert.deepEqual(JSON.parse(outputHealthJson('switch', payload, 'darwin')), { verdict: 'ok', action: 'switch' });
	assert.deepEqual(JSON.parse(outputHealthJson('inputs', payload, 'darwin')), { verdict: 'ok', action: 'inputs' });
	assert.deepEqual(JSON.parse(outputHealthJson('inputs', null, 'linux')), {
		inputs: [],
		error: 'installed macOS shell required to list CoreAudio inputs'
	});
});

test('coreAudioOutputDevices is null off macOS', () => {
	assert.equal(coreAudioOutputDevices(scratchDir('linux'), 'linux'), null);
});

test('coreAudioOutputDevices runs list on macOS and returns the parsed array', { skip: POSIX_ONLY('a #!/bin/sh helper script') }, () => {
	const payload = payloadWithHelper(
		'if [ "$1" != list ]; then echo "expected list, got $1" >&2; exit 3; fi\n' +
			"printf '[{\"uid\":\"BuiltInHeadphoneOutputDevice\",\"name\":\"External Headphones\",\"transport\":\"builtin\",\"is_default\":true,\"muted_by_jack\":false,\"physical_uid\":\"BuiltInHeadphoneOutputDevice\"}]'"
	);
	const devices = coreAudioOutputDevices(payload, 'darwin');
	assert.ok(Array.isArray(devices));
	const first = (devices as { uid: string }[])[0];
	assert.ok(first !== undefined);
	assert.equal(first.uid, 'BuiltInHeadphoneOutputDevice');
});

test('coreAudioOutputDevices throws when the helper is missing or invalid on macOS', { skip: POSIX_ONLY('a #!/bin/sh helper script') }, () => {
	assert.throws(() => coreAudioOutputDevices(scratchDir('no-list-helper'), 'darwin'), /helper not bundled/);
	const failing = payloadWithHelper('echo boom >&2; exit 4');
	assert.throws(() => coreAudioOutputDevices(failing, 'darwin'), /helper failed: boom/);
	const garbage = payloadWithHelper('echo not-json');
	assert.throws(() => coreAudioOutputDevices(garbage, 'darwin'), /not JSON/);
	const object = payloadWithHelper('printf \'{"verdict":"ok"}\'');
	assert.throws(() => coreAudioOutputDevices(object, 'darwin'), /JSON array/);
});

test('coreAudioOutputDevices fails when the helper exceeds the spawn timeout on macOS', { skip: POSIX_ONLY('a #!/bin/sh helper script') }, () => {
	const slow = payloadWithHelper('sleep 15');
	assert.throws(() => coreAudioOutputDevices(slow, 'darwin'), /timed out|ETIMEDOUT/i);
});

test('a failing or non-JSON helper is unknown, never a verdict', { skip: POSIX_ONLY('a #!/bin/sh helper script') }, () => {
	const failing = payloadWithHelper('echo boom >&2; exit 4');
	assert.match(String((JSON.parse(outputHealthJson('probe', failing, 'darwin')) as { reason: string }).reason), /helper failed: boom/);
	const garbage = payloadWithHelper('echo not-json');
	const body = JSON.parse(outputHealthJson('probe', garbage, 'darwin')) as Record<string, unknown>;
	assert.equal(body.verdict, 'unknown');
	assert.match(String(body.reason), /not JSON/);
});

test('reconnect runs the helper with reconnect, asynchronously, and refuses honestly (AUDIO-DEVICE-05)', async () => {
	const payload = payloadWithHelper('printf \'{"reconnected":true,"action":"%s"}\' "$1"');
	assert.deepEqual(JSON.parse(await reconnectSpeakerJson(payload, 'darwin')), { reconnected: true, action: 'reconnect' });
	const off = JSON.parse(await reconnectSpeakerJson(payload, 'linux')) as Record<string, unknown>;
	assert.equal(off.reconnected, false, 'if reconnect claims success off macOS then it is broken');
	const missing = JSON.parse(await reconnectSpeakerJson(scratchDir('no-reconnect-helper'), 'darwin')) as Record<string, unknown>;
	assert.match(String(missing.error), /helper not bundled/);
	const failing = JSON.parse(await reconnectSpeakerJson(payloadWithHelper('echo no-blueutil >&2; exit 4'), 'darwin')) as Record<string, unknown>;
	assert.equal(failing.reconnected, false);
	assert.match(String(failing.error), /no-blueutil/);
});
