import assert from 'node:assert/strict';
import test from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

test('unchecked or absent rows are not loadable; a present row is', async () => {
	const wire = await loadTypeScriptModule(
		'src/lib/components/rb/browser/browser-row-wire.ts'
	);
	const missing = wire.libraryAudioLoadRefusal({
		file_exists: null,
		file_availability: 'AVAILABILITY_PENDING'
	});
	assert.equal(typeof missing, 'string');
	assert.match(missing, /not been confirmed/);

	const absent = wire.libraryAudioLoadRefusal({
		file_exists: false,
		file_availability: 'absent'
	});
	assert.match(absent, /missing on disk/);

	const present = wire.libraryAudioLoadRefusal({
		file_exists: true,
		file_availability: 'present'
	});
	assert.equal(present, null);
});
