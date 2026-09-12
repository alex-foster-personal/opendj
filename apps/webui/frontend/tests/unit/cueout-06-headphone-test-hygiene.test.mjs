// requirement: CUEOUT-06
// [if] the "+ OUT" control's accessible name changes again [then] the probe fails on the name lookup within seconds, not on a 120 s timeout
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const FRONTEND = fileURLToPath(new URL('../..', import.meta.url));
const PROBE = `${FRONTEND}/tests/e2e/performance-headphone-device-probe.spec.ts`;
const CLUSTER = `${FRONTEND}/src/lib/components/rb/mixer/HeadphoneCluster.svelte`;

const ACCESSIBLE_NAME = 'ADD OUTPUT';

test('HeadphoneCluster and the device probe agree on the + OUT accessible name', () => {
	const cluster = readFileSync(CLUSTER, 'utf8');
	const probe = readFileSync(PROBE, 'utf8');
	assert.match(cluster, new RegExp(`aria-label="${ACCESSIBLE_NAME}"`));
	assert.match(probe, new RegExp(`name: '${ACCESSIBLE_NAME}'`));
	assert.doesNotMatch(
		probe,
		/name: '\+ OUT'/,
		'probe must target the accessible name, not the visible glyph'
	);
	assert.match(probe, /timeout:\s*5_?000/, 'name lookup must fail within seconds, not the 120s test timeout');
});
