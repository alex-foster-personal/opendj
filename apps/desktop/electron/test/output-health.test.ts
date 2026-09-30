import * as assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { test } from 'node:test';

import { OUTPUT_PROBE_HELPER, outputHealthJson } from '../src/output-health';
import { scratchDir } from './helpers';

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

test('on macOS the helper answers, with the action passed through', () => {
	const payload = payloadWithHelper('printf \'{"verdict":"ok","action":"%s"}\' "$1"');
	assert.deepEqual(JSON.parse(outputHealthJson('probe', payload, 'darwin')), { verdict: 'ok', action: 'probe' });
	assert.deepEqual(JSON.parse(outputHealthJson('switch', payload, 'darwin')), { verdict: 'ok', action: 'switch' });
});

test('a failing or non-JSON helper is unknown, never a verdict', () => {
	const failing = payloadWithHelper('echo boom >&2; exit 4');
	assert.match(String((JSON.parse(outputHealthJson('probe', failing, 'darwin')) as { reason: string }).reason), /helper failed: boom/);
	const garbage = payloadWithHelper('echo not-json');
	const body = JSON.parse(outputHealthJson('probe', garbage, 'darwin')) as Record<string, unknown>;
	assert.equal(body.verdict, 'unknown');
	assert.match(String(body.reason), /not JSON/);
});
