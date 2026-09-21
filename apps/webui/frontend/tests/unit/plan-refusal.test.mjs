import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import {
	assertRefusalDistinctFromCapabilityRefusals,
	readCapabilitiesSource
} from './capabilities-source.mjs';

// ENT-03 - "not on your plan" is a THIRD distinct UI state.
//
// The sibling of inert-controls.test.mjs, which pins the PARITY-TODO wording.
// That test guards one of the three sentences a dead control can carry; this
// one guards the newest, and guards all three APART. The three facts are:
//
//   1. not built            'not implemented - see PARITY-TODO'   (INERT_TITLE)
//   2. this daemon does not offer it   the capabilities.svelte.ts refusals
//   3. not on your plan     PLAN_REFUSAL_TITLE, pinned below
//
// Telling a user their own software is "not implemented" when the truth is
// "you did not pay for it" is a lie about why the control is dead, and the
// three strings drifting toward each other is exactly how that happens.
//
// Regression lines:
// - if PLAN_REFUSAL_TITLE is reworded on either side of the wire then the
//   tooltip and the server's refusal have split in two
// - if the plan wording collides with INERT_TITLE or a capability refusal
//   then the three states have merged into one sentence
// - if a Svelte control hardcodes plan wording instead of asking planRefusal
//   then the rule has left the one module that enforces it

const HERE = fileURLToPath(new URL('.', import.meta.url));
const SRC = join(HERE, '../../src');
const REPO_ROOT = join(HERE, '../../../../..');

const INERT_TITLE = 'not implemented - see PARITY-TODO';

/** The one plan-refusal sentence. Spelled here as a literal on purpose: a
 * test that imported the constant it is pinning would assert nothing. */
const PLAN_REFUSAL_TITLE = 'not included in your plan - see your account for what is included';

const CLIENT_MODULE = join(SRC, 'lib/api/entitlements.svelte.ts');
const SERVER_MODULE = join(REPO_ROOT, 'apps/entitlements/resolver.py');

function read(path) {
	return readFileSync(path, 'utf8');
}

test('the frontend declares the plan refusal title verbatim', () => {
	const source = read(CLIENT_MODULE);
	assert.ok(
		source.includes(`'${PLAN_REFUSAL_TITLE}'`),
		'entitlements.svelte.ts must declare PLAN_REFUSAL_TITLE exactly as this test spells it'
	);
});

test('the daemon spells the identical sentence, so the two halves cannot drift', () => {
	// ENT-02: the disabled control and the server refusing the request read the
	// same words. The server is the source of truth at runtime; this pins the
	// pre-response fallback against it.
	const source = read(SERVER_MODULE);
	assert.ok(
		source.includes(PLAN_REFUSAL_TITLE),
		`apps/entitlements/resolver.py must hold the identical UI_REFUSAL_TITLE:\n  ${PLAN_REFUSAL_TITLE}`
	);
});

test('the plan refusal is not the PARITY-TODO wording', () => {
	assert.notEqual(PLAN_REFUSAL_TITLE, INERT_TITLE);
	const lowered = PLAN_REFUSAL_TITLE.toLowerCase();
	// Not merely a different string: it must not borrow the other state's
	// vocabulary, which is what makes a user misread which fact they are told.
	assert.ok(!lowered.includes('parity'), 'plan wording must not mention PARITY-TODO');
	assert.ok(!lowered.includes('not implemented'), 'a plan-gated feature IS implemented');
});

test('the plan refusal is none of the capability refusals', () => {
	const source = readCapabilitiesSource();
	assertRefusalDistinctFromCapabilityRefusals(source, PLAN_REFUSAL_TITLE);
	// The capability refusals are all about the DAEMON; the plan refusal is
	// about the ACCOUNT. If the plan sentence starts talking about daemons the
	// two states have merged.
	assert.ok(
		!PLAN_REFUSAL_TITLE.toLowerCase().includes('daemon'),
		'plan wording must not blame the daemon; that is the capability state'
	);
	assert.ok(
		PLAN_REFUSAL_TITLE.toLowerCase().includes('plan'),
		'plan wording must actually say plan, or the user cannot tell the states apart'
	);
});

test('the plan refusal points somewhere a user can actually go', () => {
	// It names the account panel, which ACCT-01 makes reachable from the user
	// bauble. A refusal with no next step is a dead end.
	assert.ok(
		PLAN_REFUSAL_TITLE.toLowerCase().includes('account'),
		'the refusal must point at the account surface'
	);
});

test('the entitlement client never composes its own refusal sentence', () => {
	// ENT-02 mechanically: planRefusal must return the SERVER's ui_title once
	// loaded, so a control and a 4xx body cannot disagree.
	const source = read(CLIENT_MODULE);
	assert.ok(
		/return entitlements\.refusalTitle;/.test(source),
		'planRefusal must return the server-supplied refusalTitle'
	);
});

test('the bauble menu offers a route to account information', () => {
	// ACCT-01: this menu offered only "Sign out", which left identity, plan and
	// the local-data disclosure reachable by terminal only.
	const source = read(join(SRC, 'lib/components/UserBauble.svelte'));
	assert.ok(
		source.includes('openAccountOverlay'),
		'UserBauble must open the account overlay'
	);
	assert.ok(/>\s*Account\s*</.test(source), 'the menu must carry a visible Account item');
});

test('the account overlay is mounted at the root layout', () => {
	// Same reason SetupOverlay and SettingsOverlay are: /performance bypasses
	// the app shell, and the bauble is drawn there too.
	const source = read(join(SRC, 'routes/+layout.svelte'));
	assert.ok(source.includes('<AccountOverlay />'), 'root layout must mount AccountOverlay');
});
