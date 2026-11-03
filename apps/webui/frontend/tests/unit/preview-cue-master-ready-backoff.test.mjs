import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const WATCHDOG = 'src/lib/rb/audio-context-watchdog.ts';
const SUPPORT = 'tests/e2e/support/preview-cue-master-ready.ts';

test('if the preview-cue support backoff literal drifts from the product watchdog then broken', async () => {
	const { CONTEXT_RESUME_BACKOFF_MS } = await loadTypeScriptModule(WATCHDOG);
	const source = readFileSync(SUPPORT, 'utf8');
	const match = source.match(/const CONTEXT_RESUME_BACKOFF_MS: readonly number\[\] = \[([^\]]*)\]/);
	assert.ok(match, 'the support file must declare the backoff literal');
	const literal = match[1].split(',').map((n) => Number(n.replaceAll('_', '').trim()));
	assert.deepEqual(literal, [...CONTEXT_RESUME_BACKOFF_MS]);
});

test('if the backoff literal is not compared to a non-empty array then the pin is vacuous', () => {
	// Control: a literal that is missing or empty must not pass the same parse.
	const parse = (text) => text.split(',').map((n) => Number(n.replaceAll('_', '').trim()));
	assert.deepEqual(parse('0, 150, 400'), [0, 150, 400]);
	assert.notDeepEqual(parse('0'), [0, 150]);
});
