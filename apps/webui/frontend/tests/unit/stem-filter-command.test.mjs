import { test, before } from 'node:test';
import assert from 'node:assert/strict';
import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;
before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/stem-filter-command.ts');
});

test('valid payload parses', () => {
	/** [if] deck 1, stem drums, value 0.2 [then] the typed stem_filter command comes back, [else stop]. */
	assert.deepEqual(mod.parseStemFilterCommand({ deck: 1, stem: 'drums', value: 0.2 }), {
		ok: true,
		command: { type: 'stem_filter', deck: 1, stem: 'drums', value: 0.2 }
	});
});

test('bad deck, stem, value or payload is rejected', () => {
	/** [if] deck, stem or value is invalid or the payload is not an object [then] ok is false with a reason, [else stop]. */
	const bad = [
		{ deck: 0, stem: 'bass', value: 0.5 },
		{ deck: 1.5, stem: 'bass', value: 0.5 },
		{ deck: 1, stem: 'keys', value: 0.5 },
		{ deck: 1, stem: 'bass' },
		{ deck: 1, stem: 'bass', value: 1.2 },
		{ deck: 1, stem: 'bass', value: Number.NaN },
		null,
		'stem_filter'
	];
	for (const raw of bad) {
		const out = mod.parseStemFilterCommand(raw);
		assert.equal(out.ok, false);
		assert.ok(out.reason.length > 0);
	}
});
