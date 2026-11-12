import * as assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { test } from 'node:test';

import { OUTPUT_PROBE_HELPER } from './output-health';
import {
	CoreAudioListError,
	classifyCoreAudioListFailure,
	resolveListCoreAudioOutputs
} from './coreaudio-list';
import { POSIX_ONLY, scratchDir } from '../test/helpers';

function payloadWithHelper(script: string): string {
	const payload = path.join(scratchDir('main-list'), 'payload');
	const helper = path.join(payload, OUTPUT_PROBE_HELPER);
	fs.mkdirSync(path.dirname(helper), { recursive: true });
	fs.writeFileSync(helper, `#!/bin/sh\n${script}\n`, { mode: 0o755 });
	return payload;
}

test('[if] CoreAudio reports no output device [then] code names it, [else stop]', { skip: POSIX_ONLY('a #!/bin/sh helper script') }, () => {
	const payload = payloadWithHelper('echo "no output device" >&2; exit 2');
	assert.throws(
		() => resolveListCoreAudioOutputs(payload, 'darwin'),
		(error: unknown) => {
			assert.ok(CoreAudioListError.is(error));
			const listed = error as CoreAudioListError;
			assert.equal(listed.code, 'no_output_device');
			assert.match(listed.reason, /no output device/);
			assert.notEqual(listed.code, 'unknown');
			return true;
		}
	);
});

test('[if] CoreAudio returns an unknown helper failure [then] reason is preserved, [else stop]', { skip: POSIX_ONLY('a #!/bin/sh helper script') }, () => {
	const payload = payloadWithHelper('echo "explosion" >&2; exit 9');
	assert.throws(
		() => resolveListCoreAudioOutputs(payload, 'darwin'),
		(error: unknown) => {
			assert.ok(CoreAudioListError.is(error));
			const listed = error as CoreAudioListError;
			assert.equal(listed.code, 'helper_failed');
			assert.match(listed.reason, /explosion/);
			assert.doesNotMatch(listed.message, /"verdict":"unknown"/);
			return true;
		}
	);
});

test('[if] the renderer invokes the list bridge on failure [then] it gets classified error, [else stop]', { skip: POSIX_ONLY('a #!/bin/sh helper script') }, () => {
	const payload = payloadWithHelper('echo "output listing failed" >&2; exit 1');
	let caught: CoreAudioListError | null = null;
	try {
		resolveListCoreAudioOutputs(payload, 'darwin');
	} catch (error) {
		if (CoreAudioListError.is(error)) caught = error;
	}
	assert.ok(caught !== null);
	assert.equal(caught.code, 'no_output_device');
	assert.match(caught.message, /^coreaudio-list:no_output_device:/);
	assert.doesNotMatch(caught.message, /unknownProbeJson|verdict/);
});

test('resolveListCoreAudioOutputs returns a parsed array when the helper succeeds', { skip: POSIX_ONLY('a #!/bin/sh helper script') }, () => {
	const payload = payloadWithHelper(
		'if [ "$1" != list ]; then echo "expected list" >&2; exit 3; fi\n' +
			'printf \'[{"uid":"u1","name":"Speakers"}]\''
	);
	const devices = resolveListCoreAudioOutputs(payload, 'darwin');
	assert.ok(Array.isArray(devices));
	assert.equal((devices as { uid: string }[])[0]?.uid, 'u1');
});

test('classifyCoreAudioListFailure never maps helper stderr to code unknown', () => {
	const failure = classifyCoreAudioListFailure(new Error('output probe helper failed: boom'));
	assert.equal(failure.code, 'helper_failed');
	assert.equal(failure.reason, 'boom');
	assert.notEqual(failure.code, 'unknown');
});
