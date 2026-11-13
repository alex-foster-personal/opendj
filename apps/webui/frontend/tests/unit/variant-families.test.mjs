/**
 * @pytest.mark.requirement VARIANT-14064
 * [if] settings/catalog.ts is loaded alone [then] every variant family's rows are catalogue rows, once each, [else stop ⛔️]
 */
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { join, relative } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const LIB_ROOT = join(FRONTEND_ROOT, 'src/lib');
const REGISTRY_MODULE = 'src/lib/settings/variant-setting.ts';
const ON_MAIN_IDS = [
	'variant_fx_pitch_window',
	'variant_fx_stutter_chunk',
	'variant_fx_stutter_blend',
	'variant_sndcol_noise_colour'
];

/** Every `.ts` module under src/lib (never `.svelte`) that calls defineVariantSetting. */
function variantFamilyModules() {
	return readdirSync(LIB_ROOT, { recursive: true })
		.filter((path) => path.endsWith('.ts') && !path.endsWith('.d.ts'))
		.map((path) => relative(FRONTEND_ROOT, join(LIB_ROOT, path)))
		.filter((path) => path !== REGISTRY_MODULE)
		.filter((path) =>
			readFileSync(join(FRONTEND_ROOT, path), 'utf8').includes('defineVariantSetting(')
		)
		.sort();
}

/** Variant ids a family module exports, read from its own evaluated defs. */
async function exportedVariantIds(path) {
	const mod = await loadTypeScriptModule(path);
	return Object.values(mod)
		.filter((value) => value && typeof value === 'object' && value.devOnly === true)
		.map((value) => value.id)
		.filter((id) => typeof id === 'string' && id.startsWith('variant_'));
}

async function catalogueIds() {
	const { SETTINGS_CATALOG } = await loadTypeScriptModule('src/lib/settings/catalog.ts');
	return SETTINGS_CATALOG.map((row) => row.id);
}

test('catalogue loaded alone carries the four variant rows on main', async () => {
	// [if] catalog.ts is imported alone [then] the four variant ids on main are rows, [else stop].
	const ids = await catalogueIds();
	for (const id of ON_MAIN_IDS) assert.ok(ids.includes(id), `catalogue is missing ${id}`);
});

test('every defineVariantSetting module reaches the catalogue through the index', async () => {
	// [if] a src/lib .ts module defines a variant [then] its exported ids are catalogue rows, [else stop].
	const modules = variantFamilyModules();
	assert.ok(modules.length >= 3, `expected the 3 families on main, found ${modules.join(', ')}`);
	const ids = new Set(await catalogueIds());
	for (const path of modules) {
		const exported = await exportedVariantIds(path);
		assert.ok(exported.length > 0, `${path} calls defineVariantSetting but exports no def`);
		for (const id of exported) {
			assert.ok(
				ids.has(id),
				`${id} from ${path} is not a catalogue row; import it in variant-families.ts`
			);
		}
	}
});

test('no variant id appears twice in the catalogue', async () => {
	// [if] the catalogue is built [then] each variant id is one row, [else stop].
	const variantIds = (await catalogueIds()).filter((id) => id.startsWith('variant_'));
	assert.deepEqual(variantIds, [...new Set(variantIds)]);
});
