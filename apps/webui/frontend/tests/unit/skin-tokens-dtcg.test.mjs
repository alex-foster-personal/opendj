/**
 * Issue #13871: DTCG export/import for scheme tokens.
 *
 * Regression lines:
 * - if fromDtcg(toDtcg(DARK_SCHEME_TOKENS)) differs from DARK_SCHEME_TOKENS then the round-trip test fails
 * - if a DTCG token has a $type other than color or dimension then fromDtcg throws naming the token path and type
 * - if a color $value is not a hex colour then fromDtcg throws naming the token path
 * - if an imported scheme fails validateScheme then fromDtcg returns the violations, not a silent pass
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let dt;
let tt;
before(async () => {
	dt = await loadTypeScriptModule('src/lib/rb/skin-tokens-dtcg.ts');
	tt = await loadTypeScriptModule('src/lib/rb/theme-tokens.ts');
});

test('round-trips dark and light schemes with no violations', () => {
	for (const scheme of [tt.DARK_SCHEME_TOKENS, tt.LIGHT_SCHEME_TOKENS]) {
		const doc = dt.toDtcg(scheme);
		for (const tok of Object.values(doc)) assert.equal(tok.$type, 'color');
		const back = dt.fromDtcg(JSON.parse(JSON.stringify(doc)));
		assert.deepEqual(back.tokens, scheme);
		assert.deepEqual(back.violations, []);
	}
});

test('unknown $type throws naming path and type', () => {
	const doc = { rb: { bg: { $type: 'fontFamily', $value: 'Inter' } } };
	assert.throws(() => dt.fromDtcg(doc), /rb\.bg.*fontFamily/);
});

test('dimension tokens are accepted and kept apart', () => {
	const doc = {
		...dt.toDtcg(tt.DARK_SCHEME_TOKENS),
		'rb-gap': { $type: 'dimension', $value: '4px' }
	};
	const out = dt.fromDtcg(doc);
	assert.equal(out.dimensions['rb-gap'], '4px');
	assert.equal(out.tokens['rb-gap'], undefined);
});

test('non-hex colour throws naming the path', () => {
	const doc = { rb: { text: { $type: 'color', $value: 'rgb(1,2,3)' } } };
	assert.throws(() => dt.fromDtcg(doc), /rb\.text/);
});

test('low-contrast import returns violations', () => {
	const scheme = { ...tt.DARK_SCHEME_TOKENS, 'rb-text': tt.DARK_SCHEME_TOKENS['rb-bg'] };
	const out = dt.fromDtcg(dt.toDtcg(scheme));
	assert.ok(out.violations.length > 0);
	assert.ok(out.violations.some((v) => v.pairing.fg === 'rb-text' || v.pairing.bg === 'rb-text'));
});
