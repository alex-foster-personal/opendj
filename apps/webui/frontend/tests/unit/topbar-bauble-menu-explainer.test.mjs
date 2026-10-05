/**
 * requirement: CHROME-05
 *
 * Codex BLOCKING on PR #3896 (comment 4132382111): signed in, a hover then a
 * click on the top-bar bauble left TopBarAccountCluster's ControlExplainer
 * open while UserBauble opened its account menu. Both sit below the same
 * trigger, the explainer at z-index 80 over the menu's 40, and the menu is a
 * DESCENDANT of the explainer wrapper, so moving onto it never fires the
 * wrapper's pointerleave. The explainer covered the menu, Sign out included.
 *
 * The fix relays the bauble's `menuOpen` (now a bindable prop) through TopBar
 * into the cluster, which passes no bullets while it is true. ControlExplainer
 * renders its popover only under `{#if open && hasRich}`, and with no bullets,
 * warning, action or demo `hasRich` is false.
 *
 * No DOM in this suite (see load-svelte-ssr.mjs), so a hover cannot be
 * driven. Instead the popover gate is EXECUTED: the cluster's own `bullets`
 * expression and ControlExplainer's own `hasRich` and `{#if}` expressions are
 * lifted out of the real sources by Svelte's parser and evaluated, so a
 * changed expression on either side changes the verdict. The bauble half is
 * rendered through Svelte's real SSR renderer.
 *
 * [if] the pointer is on the bauble (explainer `open`) and the menu is open
 *   [then] the explainer popover does not render [else stop].
 * control: with the menu closed the same hover DOES render the popover with
 *   the login-gated bullets, so an overshoot that never shows it goes red.
 * [if] TopBar renders the bauble [then] the variable bound to its menuOpen is
 *   the one passed to the cluster [else stop: the cluster reads a constant].
 * [if] menuOpen is true on a signed-in bauble [then] the account menu with
 *   Sign out renders; control: false renders no menu.
 *
 * Not covered here: real pointer sequencing and on-screen stacking, which only
 * a browser can show.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { after, before, test } from 'node:test';
import { parse } from 'svelte/compiler';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

function load(rel) {
	const path = fileURLToPath(new URL(`../../src/lib/components/${rel}`, import.meta.url));
	const source = readFileSync(path, 'utf8');
	return { source, ast: parse(source, { modern: true, filename: path }) };
}

const cluster = load('rb/TopBarAccountCluster.svelte');
const explainer = load('rb/deck/ControlExplainer.svelte');
const topbar = load('rb/TopBar.svelte');
const bauble = load('UserBauble.svelte');

/** Every node matching `pred`, walked over the modern AST. */
function findAll(node, pred, found = []) {
	if (node === null || typeof node !== 'object') return found;
	if (Array.isArray(node)) {
		for (const child of node) findAll(child, pred, found);
		return found;
	}
	if (pred(node)) found.push(node);
	for (const [key, value] of Object.entries(node)) {
		if (key !== 'parent' && value && typeof value === 'object') findAll(value, pred, found);
	}
	return found;
}

function one(nodes, what) {
	assert.equal(nodes.length, 1, `expected exactly one ${what}, found ${nodes.length}`);
	return nodes[0];
}

const text = (src, node) => src.slice(node.start, node.end);

/** Source text of a script-level `const|let <name> = <init>` initializer. */
function initializer(file, name) {
	const decl = one(
		findAll(file.ast.instance, (n) => n.type === 'VariableDeclarator' && n.id?.name === name),
		`declaration of ${name}`
	);
	return decl.init;
}

/** Source text of the single expression an element attribute carries. */
function attrExpression(file, element, name) {
	const attr = element.attributes.find((a) => a.name === name);
	assert.ok(attr, `<${element.name}> carries ${name}`);
	assert.equal(attr.value.type, 'ExpressionTag', `${name} is one {expression}`);
	return text(file.source, attr.value.expression);
}

function evaluate(expr, scope) {
	return new Function(...Object.keys(scope), `return (${expr});`)(...Object.values(scope));
}

const clusterExplainer = one(
	findAll(cluster.ast.fragment, (n) => n.type === 'Component' && n.name === 'ControlExplainer'),
	'ControlExplainer in the cluster'
);
const bulletsExpr = attrExpression(cluster, clusterExplainer, 'bullets');
const loginGatedBullets = evaluate(text(cluster.source, initializer(cluster, 'loginGatedBullets')), {});

const hasRichInit = initializer(explainer, 'hasRich');
assert.equal(hasRichInit.callee?.name, '$derived');
const hasRichExpr = text(explainer.source, hasRichInit.arguments[0]);
const isPopDiv = (n) =>
	n.type === 'RegularElement' &&
	n.name === 'div' &&
	n.attributes.some((a) => a.name === 'class' && text(explainer.source, a) === 'class="pop"');
// The {#if} whose own body holds the popover div, not an outer one.
const popoverGate = one(
	findAll(explainer.ast.fragment, (n) => n.type === 'IfBlock' && n.consequent.nodes.some(isPopDiv)),
	'{#if} gating the explainer popover'
);
const popoverGateExpr = text(explainer.source, popoverGate.test);

/**
 * Does the explainer popover render for this hover/menu state? Runs the
 * cluster's bullets expression, then ControlExplainer's hasRich and its
 * popover {#if}, all as written in the sources.
 */
function popoverRenders({ hovered, menuOpen }) {
	const bullets = evaluate(bulletsExpr, { menuOpen, loginGatedBullets });
	// The cluster passes none of the other rich props, so they sit at their
	// null defaults; asserted below rather than assumed.
	// `disabled` (added for IOPIN on #3837) is not passed by the cluster either,
	// so it sits at its false default; asserted below as well.
	const hasRich = evaluate(hasRichExpr, { bullets, warning: null, action: null, demo: null, disabled: false });
	return { renders: Boolean(evaluate(popoverGateExpr, { open: hovered, hasRich })), bullets };
}

test('the cluster explainer carries no warning, action or demo that would keep it rich', () => {
	for (const name of ['warning', 'action', 'demo']) {
		assert.equal(
			clusterExplainer.attributes.some((a) => a.name === name),
			false,
			`${name} would make hasRich true regardless of bullets`
		);
	}
	for (const name of ['warning', 'action', 'demo']) {
		assert.match(explainer.source, new RegExp(`\\b${name} = null,`), `${name} defaults to null`);
	}
	assert.equal(clusterExplainer.attributes.some((a) => a.name === 'disabled'), false, 'disabled would suppress the explainer');
	assert.match(explainer.source, /\bdisabled = false,/, 'disabled defaults to false');
});

test('while the account menu is open, hovering the bauble renders no explainer popover', () => {
	assert.equal(popoverRenders({ hovered: true, menuOpen: true }).renders, false);
});

test('control: with the menu closed, the same hover renders the login-gated explainer', () => {
	const { renders, bullets } = popoverRenders({ hovered: true, menuOpen: false });
	assert.equal(renders, true, 'the explainer must still teach the login-gated features');
	assert.deepEqual([...bullets], loginGatedBullets);
	assert.ok(loginGatedBullets.length > 0);
	// And the instrument can say "hidden" for the ordinary reason too.
	assert.equal(popoverRenders({ hovered: false, menuOpen: false }).renders, false);
});

test('TopBar feeds the cluster the same variable the bauble binds its menu to', () => {
	const baubleNode = one(
		findAll(topbar.ast.fragment, (n) => n.type === 'Component' && n.name === 'UserBauble'),
		'UserBauble in TopBar'
	);
	const bind = baubleNode.attributes.find((a) => a.type === 'BindDirective' && a.name === 'menuOpen');
	assert.ok(bind, 'UserBauble binds menuOpen');
	const relayed = text(topbar.source, bind.expression);
	const clusterNode = one(
		findAll(topbar.ast.fragment, (n) => n.type === 'Component' && n.name === 'TopBarAccountCluster'),
		'TopBarAccountCluster in TopBar'
	);
	assert.equal(attrExpression(topbar, clusterNode, 'menuOpen'), relayed);
	assert.equal(initializer(topbar, relayed).callee?.name, '$state', `${relayed} is reactive state`);
	// The cluster reads the prop it is given, not a local constant.
	const props = one(
		findAll(cluster.ast.instance, (n) => n.type === 'ObjectPattern' && n.properties.some((p) => p.key?.name === 'children')),
		'cluster $props pattern'
	);
	assert.ok(props.properties.some((p) => p.key?.name === 'menuOpen'), 'cluster takes menuOpen as a prop');
});

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
after(() => {
	if (mod) mod.auth.user = null;
});

test('the bauble menu is driven by the bindable menuOpen prop the cluster reads', () => {
	assert.match(bauble.source, /menuOpen = \$bindable\(false\)/);
	mod.auth.user = { email: 'dj@example.test', name: 'Test DJ', avatar_url: null };
	const open = mod.render(mod.UserBauble, { props: { size: 20, showLabel: true, menuOpen: true } }).body;
	assert.match(open, /role="menu"/);
	assert.match(open, />\s*Sign out\s*</);
	assert.match(open, /aria-expanded="true"/);
	// Control: the same signed-in render with the prop false has no menu.
	const closed = mod.render(mod.UserBauble, { props: { size: 20, showLabel: true, menuOpen: false } }).body;
	assert.doesNotMatch(closed, /role="menu"/);
	assert.match(closed, /aria-label="Signed in as dj@example\.test"/, 'the signed-in control still rendered');
});
