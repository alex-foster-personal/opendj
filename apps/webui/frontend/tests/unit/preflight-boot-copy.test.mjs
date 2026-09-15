/**
 * Boot gate heading escalation (issue #2722 P1-4, P2-2).
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

function check(overrides = {}) {
	return {
		id: 'engine-alive',
		label: 'Engine alive',
		status: 'pass',
		detail: 'the endpoint answered',
		remediation: null,
		...overrides
	};
}

test('bootGateHeading reflects blocked library state and escalates after repeated fails', async () => {
	const { bootGateHeading, BOOT_BLOCKED_HEADING, BOOT_ESCALATED_HEADING, BOOT_STARTING_HEADING } =
		await loadTypeScriptModule('src/lib/preflight/boot-copy.ts', {
			viteApiBase: 'https://preflight-boot-copy.example.test'
		});

	const emptyLibrary = [
		check(),
		check({ id: 'library-attached', label: 'Library attached', status: 'fail', detail: '0 tracks' })
	];

	assert.equal(bootGateHeading(true, emptyLibrary, 0), BOOT_BLOCKED_HEADING);
	assert.equal(bootGateHeading(true, emptyLibrary, 2), BOOT_BLOCKED_HEADING);
	assert.equal(bootGateHeading(true, emptyLibrary, 3), BOOT_ESCALATED_HEADING);
	assert.equal(bootGateHeading(true, [check({ id: 'state-db', status: 'fail' })], 0), BOOT_STARTING_HEADING);
	assert.equal(bootGateHeading(false, emptyLibrary, 5), 'Startup checks');
});
