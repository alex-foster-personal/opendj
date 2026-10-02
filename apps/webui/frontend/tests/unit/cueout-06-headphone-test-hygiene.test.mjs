// requirement: CUEOUT-06
// [if] the I/O control's accessible name is not SHOW AUDIO I/O [then] the probe fails on the name lookup within seconds, not on a 120 s timeout
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const FRONTEND = fileURLToPath(new URL('../..', import.meta.url));
const PROBE = `${FRONTEND}/tests/e2e/performance-headphone-device-probe.spec.ts`;
const CLUSTER = `${FRONTEND}/src/lib/components/rb/mixer/HeadphoneCluster.svelte`;

const ACCESSIBLE_NAME = 'SHOW AUDIO I/O';

test('HeadphoneCluster and the device probe agree on the I/O accessible name', () => {
	const cluster = readFileSync(CLUSTER, 'utf8');
	const probe = readFileSync(PROBE, 'utf8');
	assert.match(cluster, new RegExp(`aria-label="${ACCESSIBLE_NAME}"`));
	assert.match(cluster, /><span>SET<\/span><span>OUTPUTS<\/span></, 'pin 894af5672c3b: visible text is SET OUTPUTS');
	assert.match(probe, new RegExp(`name: '${ACCESSIBLE_NAME}'`));
	assert.doesNotMatch(
		probe,
		/name: '\+ OUT'/,
		'probe must target the accessible name, not a visible glyph'
	);
	assert.doesNotMatch(probe, /name: 'ADD OUTPUT'/);
	assert.match(probe, /timeout:\s*5_?000/, 'name lookup must fail within seconds, not the 120s test timeout');
});
