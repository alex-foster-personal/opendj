/**
 * Bounded AudioContext IO waits for clockless default outputs (#2150).
 *
 * [if] the promise resolves before timeoutMs [then] the helper returns that value and does not reject
 * [if] the promise never settles [then] it rejects AudioContextIoTimeoutError after timeoutMs, message contains timed out after
 * [if] timeoutMs is 0 or non-finite [then] RangeError
 * [if] the promise rejects first [then] that error propagates, not a timeout
 */
import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

describe('withAudioContextIoTimeout', () => {
	let mod;

	before(async () => {
		mod = await loadTypeScriptModule('src/lib/rb/audio-context-io-timeout.ts');
	});

	it('returns the resolved value when the promise settles first', async () => {
		const result = await mod.withAudioContextIoTimeout('resume', Promise.resolve('ok'), 50);
		assert.equal(result, 'ok');
	});

	it('rejects AudioContextIoTimeoutError when the promise never settles', async () => {
		await assert.rejects(
			mod.withAudioContextIoTimeout('resume', new Promise(() => {}), 30),
			(err) => {
				assert.ok(err instanceof mod.AudioContextIoTimeoutError);
				assert.match(err.message, /timed out after 30ms/);
				assert.equal(err.operation, 'resume');
				return true;
			}
		);
	});

	it('rejects RangeError for 0 or non-finite timeoutMs', async () => {
		await assert.rejects(
			() => mod.withAudioContextIoTimeout('resume', Promise.resolve(), 0),
			RangeError
		);
		await assert.rejects(
			() => mod.withAudioContextIoTimeout('resume', Promise.resolve(), Number.NaN),
			RangeError
		);
	});

	it('propagates the original rejection when the promise rejects first', async () => {
		const boom = new Error('device gone');
		await assert.rejects(
			mod.withAudioContextIoTimeout('suspend', Promise.reject(boom), 50),
			(err) => err === boom
		);
	});
});
