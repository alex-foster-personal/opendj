// D8: extra engine processes (the Rust audio engine) from payload/sidecars.json.
import * as assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { test } from 'node:test';

import { readSidecarManifest, sidecarGlobals, startSidecars, stopSidecars } from '../src/sidecars';
import { fakePayload, pidGone, scratchDir, waitFor } from './helpers';

function manifest(payload: string, body: unknown): void {
	fs.mkdirSync(payload, { recursive: true });
	fs.writeFileSync(path.join(payload, 'sidecars.json'), JSON.stringify(body));
}

const AUDIO = { name: 'audio-engine', launcher: 'bin/odj-audio-engine', origin_global: 'OPENDJ_AUDIO_ENGINE_ORIGIN' };

test('no manifest means no sidecars', () => {
	assert.deepEqual(readSidecarManifest(scratchDir('no-manifest')), []);
});

test('a valid manifest is read, required defaulting to false', () => {
	const payload = path.join(scratchDir('manifest'), 'payload');
	manifest(payload, { sidecars: [AUDIO] });
	assert.deepEqual(readSidecarManifest(payload), [
		{ name: 'audio-engine', launcher: 'bin/odj-audio-engine', originGlobal: 'OPENDJ_AUDIO_ENGINE_ORIGIN', required: false }
	]);
});

test('a broken manifest fails loud instead of shipping without the sidecar', () => {
	const payload = path.join(scratchDir('bad-manifest'), 'payload');
	const cases: [unknown, RegExp][] = [
		[{ sidecars: 'x' }, /expected/],
		[{ sidecars: [{ ...AUDIO, launcher: '../../bin/sh' }] }, /relative path inside the payload/],
		[{ sidecars: [{ ...AUDIO, launcher: '/bin/sh' }] }, /relative path inside the payload/],
		[{ sidecars: [{ ...AUDIO, name: 'engine' }] }, /is taken/],
		[{ sidecars: [AUDIO, AUDIO] }, /is taken/],
		[{ sidecars: [{ ...AUDIO, origin_global: 'OPENDJ_ENGINE_ORIGIN' }] }, /origin_global/],
		[{ sidecars: [{ ...AUDIO, origin_global: 'location' }] }, /origin_global/],
		[{ sidecars: [{ ...AUDIO, required: 'yes' }] }, /boolean/]
	];
	for (const [body, pattern] of cases) {
		manifest(payload, body);
		assert.throws(() => readSidecarManifest(payload), pattern, JSON.stringify(body));
	}
	fs.writeFileSync(path.join(payload, 'sidecars.json'), '{not json');
	assert.throws(() => readSidecarManifest(payload), /not JSON/);
});

test('a healthy sidecar starts with the engine contract and its origin is injected', async () => {
	const dir = scratchDir('sidecar-up');
	const payload = fakePayload(dir, 'healthy', 'bin/odj-audio-engine');
	const logDir = path.join(dir, 'data/logs');
	const running = await startSidecars(payload, path.join(dir, 'data'), logDir, [
		{ name: 'audio-engine', launcher: 'bin/odj-audio-engine', originGlobal: 'OPENDJ_AUDIO_ENGINE_ORIGIN', required: false }
	]);
	try {
		assert.equal(running.length, 1);
		const origin = running[0]?.engine.origin() as string;
		assert.deepEqual(sidecarGlobals(running), { OPENDJ_AUDIO_ENGINE_ORIGIN: origin });
		const log = fs.readFileSync(path.join(logDir, 'audio-engine.log'), 'utf8');
		assert.match(log, /"--data-dir".*"--host","127.0.0.1","--port"/);
		assert.match(log, new RegExp(`OPENDJ_PARENT_PID=${process.pid}`));
	} finally {
		await stopSidecars(running);
	}
	const pid = running[0]?.engine.pid as number;
	assert.ok(await waitFor(() => pidGone(pid)));
	assert.deepEqual(sidecarGlobals(running), {}, 'a dead sidecar is not advertised');
});

test('an optional sidecar that fails is skipped; a required one fails the launch', async () => {
	const dir = scratchDir('sidecar-fail');
	const payload = fakePayload(dir, 'exit', 'bin/odj-audio-engine');
	const spec = { name: 'audio-engine', launcher: 'bin/odj-audio-engine', originGlobal: 'OPENDJ_AUDIO_ENGINE_ORIGIN' };
	const skipped = await startSidecars(payload, path.join(dir, 'data'), path.join(dir, 'data/logs'), [{ ...spec, required: false }]);
	assert.deepEqual(skipped, []);
	await assert.rejects(
		startSidecars(payload, path.join(dir, 'data'), path.join(dir, 'data/logs'), [{ ...spec, required: true }]),
		/audio-engine stopped while starting up/
	);
});
