/**
 * @pytest.mark.requirement VARIANT-10527
 * [if] a variant setting is defined badly [then] module load throws naming the id, [else stop ⛔️]
 * [if] a pick is saved [then] readVariant and apply round-trip it, [else stop ⛔️]
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const STORAGE_KEY = 'mdt.rb.ui-prefs.v1';
const TEST_ID = 'variant_test_echo';

function makeLocalStorage(seed) {
	const map = new Map();
	let setCount = 0;
	return {
		map,
		storage: {
			getItem: (key) => (map.has(key) ? map.get(key) : null),
			setItem: (key, value) => {
				setCount += 1;
				map.set(key, String(value));
			},
			removeItem: (key) => map.delete(key),
			get setCount() {
				return setCount;
			}
		}
	};
}

async function loadEntry(blob) {
	const { map, storage } = makeLocalStorage();
	globalThis.window = {
		localStorage: storage,
		addEventListener() {},
		removeEventListener() {}
	};
	if (blob !== undefined) {
		map.set(STORAGE_KEY, JSON.stringify(blob));
	}
	const mod = await loadTypeScriptModule('tests/unit/variant-setting-entry.mjs');
	return { mod, map, storage };
}

test('defineVariantSetting validates at module load', async () => {
	const vs = await loadTypeScriptModule('src/lib/settings/variant-setting.ts');

	const base = {
		id: 'variant_test_x',
		label: 'T',
		group: 'advanced',
		keywords: ['t'],
		detail: 'd.',
		variants: [
			{ value: 'a', label: 'A', summary: '1' },
			{ value: 'b', label: 'B', summary: '2' }
		],
		defaultValue: 'a'
	};

	assert.throws(
		() =>
			vs.defineVariantSetting({
				...base,
				variants: [{ value: 'a', label: 'A', summary: '1' }]
			}),
		/variant setting variant_test_x:/
	);
	assert.throws(
		() =>
			vs.defineVariantSetting({
				...base,
				variants: [
					{ value: 'a', label: 'A', summary: '1' },
					{ value: 'b', label: 'B', summary: '2' },
					{ value: 'c', label: 'C', summary: '3' },
					{ value: 'd', label: 'D', summary: '4' },
					{ value: 'e', label: 'E', summary: '5' }
				],
				defaultValue: 'a'
			}),
		/variant setting variant_test_x:/
	);
	assert.throws(
		() =>
			vs.defineVariantSetting({
				...base,
				variants: [
					{ value: 'a', label: 'A', summary: '1' },
					{ value: 'a', label: 'B', summary: '2' }
				],
				defaultValue: 'a'
			}),
		/duplicate variant value/
	);
	assert.throws(() => vs.defineVariantSetting({ ...base, defaultValue: 'z' }), /defaultValue/);
	assert.throws(
		() => vs.defineVariantSetting({ ...base, id: 'bad_id' }),
		/variant_<family>_<name>/
	);

	vs.defineVariantSetting({ ...base, id: 'variant_test_two' });
	assert.throws(
		() => vs.defineVariantSetting({ ...base, id: 'variant_test_two' }),
		/already defined/
	);

	const four = vs.defineVariantSetting({
		...base,
		id: 'variant_test_four',
		variants: [
			{ value: 'a', label: 'A', summary: '1' },
			{ value: 'b', label: 'B', summary: '2' },
			{ value: 'c', label: 'C', summary: '3' },
			{ value: 'd', label: 'D', summary: '4' }
		],
		defaultValue: 'd'
	});
	assert.equal(four.control.options.length, 4);
});

test('readVariant returns default until saved, then persisted value', async () => {
	const { mod } = await loadEntry();
	assert.equal(mod.prefs.readVariant(TEST_ID), 'b');
	assert.throws(() => mod.prefs.readVariant('variant_nope_x'), /unknown variant setting/);

	mod.prefs.setVariant(TEST_ID, 'c');
	assert.equal(mod.prefs.readVariant(TEST_ID), 'c');

	const raw = globalThis.window.localStorage.getItem(STORAGE_KEY);
	const parsed = JSON.parse(raw);
	assert.equal(parsed.variants[TEST_ID], 'c');

	const reloaded = await loadEntry(parsed);
	assert.equal(reloaded.mod.prefs.readVariant(TEST_ID), 'c');
});

test('persisted value outside options throws on load', async () => {
	const { mod, map } = await loadEntry();
	mod.prefs.setShowDevUi(true);
	const base = JSON.parse(map.get(STORAGE_KEY));

	await assert.rejects(
		() => loadEntry({ ...base, variants: { [TEST_ID]: 'zzz' } }),
		(err) =>
			/variant_test_echo/.test(err.message) &&
			/zzz/.test(err.message) &&
			/a\|b\|c/.test(err.message)
	);
	await assert.rejects(() => loadEntry({ ...base, variants: 'x' }), /variants must be an object/);
	await assert.rejects(
		() => loadEntry({ ...base, variants: { [TEST_ID]: 3 } }),
		/must be a string/
	);

	const kept = await loadEntry({ ...base, variants: { variant_gone_x: 'q' } });
	const raw = kept.map.get(STORAGE_KEY);
	assert.equal(JSON.parse(raw).variants.variant_gone_x, 'q');
});

test('catalog rows are devOnly enum and hidden without show_dev_ui', async () => {
	const { mod } = await loadEntry();
	for (const def of mod.variantSetting.VARIANT_REGISTRY) {
		const cat = mod.catalog.SETTINGS_CATALOG.find((row) => row.id === def.id);
		assert.ok(cat, `${def.id} missing from SETTINGS_CATALOG`);
		assert.equal(cat.devOnly, true);
		assert.equal(cat.implemented, true);
		assert.equal(cat.control.kind, 'enum');
		assert.deepEqual(
			cat.control.options,
			def.variants.map(({ value, label }) => ({ value, label }))
		);
		assert.equal(mod.search.settingRowVisible(def, { hideTodo: true, showDev: false }), false);
		assert.equal(mod.search.settingRowVisible(def, { hideTodo: true, showDev: true }), true);
	}
	assert.equal(mod.TEST_VARIANT.id, TEST_ID);
});

test('onVariantChange fires once per real change', async () => {
	const { mod, storage } = await loadEntry();
	const calls = [];
	const unsub = mod.prefs.onVariantChange(TEST_ID, (id, from, to) => {
		calls.push([id, from, to]);
	});
	const before = storage.setCount;
	mod.prefs.setVariant(TEST_ID, 'c');
	assert.deepEqual(calls, [[TEST_ID, 'b', 'c']]);
	mod.prefs.setVariant(TEST_ID, 'c');
	assert.equal(calls.length, 1);
	assert.equal(storage.setCount, before + 1);
	unsub();
	mod.prefs.setVariant(TEST_ID, 'a');
	assert.equal(calls.length, 1);
	assert.throws(
		() => mod.prefs.onVariantChange('variant_nope_x', () => {}),
		/unknown variant setting/
	);
});

test('apply routes variant ids', async () => {
	const { mod } = await loadEntry();
	mod.apply.applySettingChange(TEST_ID, 'a');
	assert.equal(mod.apply.readSettingValue(TEST_ID), 'a');
	const parsed = JSON.parse(globalThis.window.localStorage.getItem(STORAGE_KEY));
	assert.equal(parsed.variants[TEST_ID], 'a');
	assert.throws(() => mod.apply.applySettingChange(TEST_ID, 'zzz'), /not one of/);
	assert.throws(
		() => mod.apply.applySettingChange(TEST_ID, true),
		/must be a string variant value/
	);
	assert.throws(
		() => mod.apply.applySettingChange('variant_unknown_x', 'a'),
		/unknown or disallowed setting key/
	);
});

test('shipped catalog variant rows are dev-only and the bare registry is empty', async () => {
	const catalog = await loadTypeScriptModule('src/lib/settings/catalog.ts');
	const variantRows = catalog.SETTINGS_CATALOG.filter((d) => d.id.startsWith('variant_'));
	assert.ok(variantRows.length > 0, 'catalog registers the variant families (#13727)');
	for (const row of variantRows) assert.equal(row.devOnly, true, `${row.id} must be devOnly`);
	const vs = await loadTypeScriptModule('src/lib/settings/variant-setting.ts');
	assert.equal(vs.VARIANT_REGISTRY.length, 0);
});
