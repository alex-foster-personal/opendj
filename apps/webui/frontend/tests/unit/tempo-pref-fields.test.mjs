/**
 * PREF-01: emptied tempo-pref number fields count as unset (issue #3333).
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let fields;

before(async () => {
	fields = await loadTypeScriptModule('src/lib/rb/tempo-pref-fields.ts');
});

test('tempoPrefFieldValue treats empty bindings as unset', () => {
	for (const raw of [undefined, null, '', Number.NaN, '140']) {
		assert.equal(
			fields.tempoPrefFieldValue(raw),
			null,
			`if ${String(raw)} is not unset then broken`
		);
	}
	assert.equal(fields.tempoPrefFieldValue(140), 140, 'if 140 is not kept then broken');
	assert.equal(fields.tempoPrefFieldValue(0), 0, 'if 0 is not kept then broken');
});

test('tempoPrefPatchValue sends null when every field is emptied', () => {
	assert.equal(
		fields.tempoPrefPatchValue(undefined, undefined, undefined),
		null,
		'if erasing all three fields does not send tempo_pref null then broken'
	);
});

test('tempoPrefPatchValue keeps explicit nulls for emptied fields', () => {
	const patch = fields.tempoPrefPatchValue(140, undefined, '');
	assert.deepEqual(
		patch,
		{ regular: 140, min: null, max: null },
		'if emptied fields are not null then broken'
	);
	assert.equal(
		JSON.stringify(patch),
		'{"regular":140,"min":null,"max":null}',
		'if the serialised patch drops min/max then broken'
	);
});

test('tempoPrefRangeInvalid ignores emptied fields', () => {
	assert.equal(
		fields.tempoPrefRangeInvalid(undefined, 130),
		false,
		'if an empty min invalidates the range then broken'
	);
	assert.equal(
		fields.tempoPrefRangeInvalid(140, 130),
		true,
		'if min above max is valid then broken'
	);
	assert.equal(
		fields.tempoPrefRangeInvalid(130, 130),
		true,
		'if min equal to max is valid then broken'
	);
	assert.equal(
		fields.tempoPrefRangeInvalid(120, 130),
		false,
		'if min below max is invalid then broken'
	);
});
