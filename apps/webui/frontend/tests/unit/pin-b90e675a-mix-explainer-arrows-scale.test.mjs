/**
 * pin b90e675a: "arrow A gets smaller and B gets bigger as it turns". The MIX
 * explainer drew two fixed-length route lines beside a turning knob, so the
 * picture never showed what the knob does to the two signals.
 *
 * The cue and master routes are now arrows that scale with the animated knob:
 * at the CUE end the cue arrow is full size and the master arrow small, at the
 * MASTER end the reverse.
 *
 * - if the two arrows scale the same way then the demo shows a volume knob,
 *   not a blend -> broken
 * - if an arrow's animation is not locked to the knob's duration and easing
 *   then the arrows drift out of step with the turn -> broken
 * - if an arrow scales from its center then its tail leaves the knob -> broken
 * - if an arrow has no head it is a line, and a line does not say "to" -> broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { EXPLAINER_DEMO_ANIMATIONS } from '../../src/lib/rb/explainer-demo-keyframes.ts';
import { extractStyleBlock } from './explainer-keyframes-parse.mjs';

const EXPLAINER = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/deck/ControlExplainer.svelte', import.meta.url)),
	'utf8'
);
const STYLE = extractStyleBlock(EXPLAINER);

function rule(selector) {
	const match = new RegExp(`\\n\\t${selector.replace('.', '\\.')} \\{([\\s\\S]*?)\\n\\t\\}`).exec(STYLE);
	assert.ok(match, `expected a ${selector} rule`);
	return match[1];
}

function keyframeScales(name) {
	const from = STYLE.indexOf(`@keyframes ${name} {`);
	assert.ok(from !== -1, `expected @keyframes ${name}`);
	const body = STYLE.slice(from, STYLE.indexOf('\n\t}\n', from));
	const ends = /0%,\s*100% \{\s*transform: scale\(([0-9.]+)\);/.exec(body);
	const mid = /50% \{\s*transform: scale\(([0-9.]+)\);/.exec(body);
	assert.ok(ends && mid, `${name} must scale at 0/100% and at 50%`);
	return { ends: Number(ends[1]), mid: Number(mid[1]) };
}

function animation(selector) {
	const match = /animation: ([a-z-]+) ([0-9.]+s) ([a-z-]+) infinite;/.exec(rule(selector));
	assert.ok(match, `${selector} must declare an infinite animation`);
	return { name: match[1], duration: match[2], easing: match[3] };
}

test('the two arrows scale in opposite directions across the knob turn', () => {
	const cue = keyframeScales('hp-arrow-cue');
	const master = keyframeScales('hp-arrow-master');
	assert.ok(cue.ends > cue.mid, 'the cue arrow is biggest at the CUE end of the turn');
	assert.ok(master.mid > master.ends, 'the master arrow is biggest at the MASTER end of the turn');
	assert.equal(cue.ends, master.mid, 'both arrows share one full size');
	assert.equal(cue.mid, master.ends, 'both arrows share one small size');
	assert.ok(cue.mid > 0, 'a small arrow stays visible: the blend never hides a signal entirely here');
});

test('the arrows are locked to the knob turn they illustrate', () => {
	const knob = animation('.mix-knob-cap');
	assert.equal(knob.name, 'mix-knob-turn');
	for (const [selector, name] of [['.cue-arrow', 'hp-arrow-cue'], ['.master-arrow', 'hp-arrow-master']]) {
		const arrow = animation(selector);
		assert.equal(arrow.name, name);
		assert.equal(arrow.duration, knob.duration, `${selector} must run as long as the knob turn`);
		assert.equal(arrow.easing, knob.easing, `${selector} must ease like the knob turn`);
	}
	// The knob is at its CUE end at 0/100% and its MASTER end at 50%.
	assert.match(STYLE, /@keyframes mix-knob-turn \{\s*0%,\s*100% \{\s*transform: rotate\(-45deg\);[\s\S]*?50% \{\s*transform: rotate\(45deg\);/);
});

test('each arrow scales from its tail, at the knob side', () => {
	assert.match(rule('.cue-arrow'), /transform-origin: 44px 30px;/);
	assert.match(rule('.master-arrow'), /transform-origin: 44px 18px;/);
});

test('each route is an arrow: a shaft and a head inside one scaled group', () => {
	const from = EXPLAINER.indexOf("demo === 'headphone-mix'");
	const markup = EXPLAINER.slice(from, EXPLAINER.indexOf('{:else if', from));
	for (const bus of ['cue', 'master']) {
		assert.match(
			markup,
			new RegExp(`<g class="hp-arrow ${bus}-arrow">\\s*<path d="[^"]+" class="hp-path ${bus}-path" />\\s*<path d="[^"]+Z" class="hp-head ${bus}-head" />\\s*</g>`),
			`the ${bus} route must be a shaft plus a closed head in one group`
		);
	}
});

test('the arrow keyframes are in the declared catalogue, transform only', () => {
	for (const name of ['hp-arrow-cue', 'hp-arrow-master']) {
		assert.deepEqual(EXPLAINER_DEMO_ANIMATIONS[name], [
			{ offset: 0, properties: ['transform'] },
			{ offset: 0.5, properties: ['transform'] },
			{ offset: 1, properties: ['transform'] }
		]);
	}
});
