/**
 * requirement: AUTH-02, CHROME-05
 *
 * Codex P1 on PR #3896 (comment 4129520589): TopBar passed
 * `showLabel={signedIn}` to the bauble, so a signed-out /performance top bar
 * carried only the avatar circle. That is the one state AUTH-02 exists for:
 * the visible "Sign in with Google" text is the only sign-in affordance on the
 * route.
 *
 * The earlier guard (bauble-label-regression.test.mjs) matched
 * `/<UserBauble[^>]*\bshowLabel\b/`, which `showLabel={signedIn}` satisfies,
 * so it could not see this. These read the attribute through Svelte's own
 * parser and render the real component through Svelte's SSR renderer.
 *
 * [if] TopBar renders the bauble [then] showLabel is the bare attribute, true
 *   in every auth state [else stop if it depends on signed-in state].
 * [if] the bauble renders signed out with that prop [then] the markup carries
 *   the visible "Sign in with Google" label on the gray (not signed-in)
 *   control [else stop].
 * control: without showLabel the same render has no label, so the render
 *   assertion can fail.
 * control (signed in): TopBar still relays signedIn from the account cluster
 *   and derives the clock from it (CHROME-06), so the fix did not drop the
 *   signed-in relay to make the label unconditional.
 *
 * Not covered here: the signed-in render itself (the store starts signed out
 * and this suite has no real session to sign in with) and CSS width, which
 * performance-topbar-responsive.spec.ts measures in a browser.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';
import { parse } from 'svelte/compiler';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const TOPBAR_PATH = fileURLToPath(
	new URL('../../src/lib/components/rb/TopBar.svelte', import.meta.url)
);
const topbarSource = readFileSync(TOPBAR_PATH, 'utf8');

/** Every template node named `name`, walked over the modern AST. */
function nodesNamed(node, name, found = []) {
	if (node === null || typeof node !== 'object') return found;
	if (Array.isArray(node)) {
		for (const child of node) nodesNamed(child, name, found);
		return found;
	}
	if (node.name === name && Array.isArray(node.attributes)) found.push(node);
	for (const [key, value] of Object.entries(node)) {
		if (key !== 'parent' && value && typeof value === 'object') nodesNamed(value, name, found);
	}
	return found;
}

const ast = parse(topbarSource, { modern: true, filename: TOPBAR_PATH });

let mod;
before(async () => {
	mod = await loadSvelteSsrModule(
		[
			"export { default as UserBauble } from '$lib/components/UserBauble.svelte';",
			"export { auth } from '$lib/auth.svelte';",
			"export { render } from 'svelte/server';"
		].join('\n')
	);
});

test('TopBar passes showLabel as the bare attribute, not a signed-in expression', () => {
	const baubles = nodesNamed(ast.fragment, 'UserBauble');
	assert.equal(baubles.length, 1, 'TopBar renders exactly one UserBauble');
	const attr = baubles[0].attributes.find((a) => a.name === 'showLabel');
	assert.ok(attr, 'UserBauble carries showLabel');
	assert.equal(attr.type, 'Attribute');
	assert.equal(
		attr.value,
		true,
		'showLabel must be true in every auth state; an expression hides it signed out'
	);
});

test('signed out, the labeled bauble renders visible "Sign in with Google" on the gray control', () => {
	assert.equal(mod.auth.user, null, 'the store starts signed out; this is the case under test');
	const html = mod.render(mod.UserBauble, { props: { size: 20, showLabel: true } }).body;
	assert.match(html, /<span class="bauble-label\b[^"]*">Sign in with Google<\/span>/);
	const button = html.slice(html.indexOf('<button'), html.indexOf('>', html.indexOf('<button')));
	assert.match(button, /class="bauble[^"]*\bpilled\b/);
	assert.doesNotMatch(button, /\bsigned-in\b/, 'signed out stays the gray face (CHROME-05)');
});

test('control: without showLabel the same signed-out render has no visible label', () => {
	const html = mod.render(mod.UserBauble, { props: { size: 20, showLabel: false } }).body;
	assert.doesNotMatch(html, /bauble-label/);
	assert.match(html, /aria-label="Sign in with Google"/, 'the control itself still rendered');
});

test('control (signed in): TopBar still relays signedIn and derives the clock from it', () => {
	const clusters = nodesNamed(ast.fragment, 'TopBarAccountCluster');
	assert.equal(clusters.length, 1);
	const bind = clusters[0].attributes.find((a) => a.type === 'BindDirective');
	assert.equal(bind?.name, 'signedIn');
	assert.match(topbarSource, /const showClock = \$derived\(\s*signedIn \|\|/);
});
