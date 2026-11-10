// requirement: A11Y-11738 (issue #10367, epic #10346)
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

const { allowlistProblems, classify, flatten, selectorOf, staleEntries, SURFACES } =
	await loadTypeScriptModule('tests/e2e/support/axe-audit.ts');

const ALLOW_PATH = fileURLToPath(new URL('../e2e/ui-axe-audit.allow.json', import.meta.url));
const REAL_ALLOW = JSON.parse(readFileSync(ALLOW_PATH, 'utf8'));

test('the real allowlist passes allowlistProblems and each entry has shape', () => {
	assert.deepEqual(allowlistProblems(REAL_ALLOW), []);
	for (const entry of REAL_ALLOW.findings) {
		assert.ok(entry.rule, 'if rule missing then shape check fails');
		assert.ok(entry.selector, 'if selector missing then shape check fails');
		for (const s of entry.surfaces) {
			assert.ok(SURFACES.includes(s), 'if unknown surface then shape check fails');
		}
		assert.ok(entry.reason.trim().length >= 20, 'if reason short then shape check fails');
		assert.match(entry.reason, /#\d{3,}/, 'if no issue ref then shape check fails');
	}
});

test('allowlistProblems rejects invalid entries', () => {
	const base = { disabledRules: [], findings: [] };
	assert.ok(
		allowlistProblems({
			...base,
			findings: [{ rule: 'x', selector: '#a', surfaces: ['settings'], reason: '' }]
		}).length
	);
	assert.ok(
		allowlistProblems({
			...base,
			findings: [
				{ rule: 'x', selector: '#a', surfaces: ['settings'], reason: 'TODO fix soon please #10346' }
			]
		}).length
	);
	assert.ok(
		allowlistProblems({
			...base,
			findings: [
				{ rule: 'x', selector: '#a', surfaces: ['settings'], reason: 'pre-existing #10346' }
			]
		}).length
	);
	assert.ok(
		allowlistProblems({
			...base,
			findings: [
				{ rule: 'x', selector: '#a', surfaces: ['settings'], reason: 'abcdefghijklmnopqrstuvwxy' }
			]
		}).length
	);
	assert.ok(
		allowlistProblems({
			...base,
			findings: [
				{
					rule: 'x',
					selector: '#a',
					surfaces: ['nope'],
					reason: 'unknown surface listed here #10346'
				}
			]
		}).length
	);
	assert.ok(
		allowlistProblems({
			...base,
			findings: [{ rule: 'x', selector: '#a', surfaces: [], reason: 'empty surfaces array #10346' }]
		}).length
	);
	assert.deepEqual(
		allowlistProblems({
			...base,
			findings: [
				{
					rule: 'dup',
					selector: '#a',
					surfaces: ['settings'],
					reason: 'first duplicate pair entry #10346'
				},
				{
					rule: 'dup',
					selector: '#a',
					surfaces: ['settings'],
					reason: 'second duplicate pair entry #10346'
				}
			]
		}).length,
		1
	);
	assert.ok(
		allowlistProblems({
			disabledRules: [
				{ rule: 'color-contrast', reason: 'disabled rule with issue reference #8527' }
			],
			findings: [
				{
					rule: 'color-contrast',
					selector: '#x',
					surfaces: ['settings'],
					reason: 'also in findings #10346'
				}
			]
		}).length
	);
});

test('classify splits unexpected, allowed and info', () => {
	const allow = {
		disabledRules: [],
		findings: [
			{
				rule: 'label',
				selector: '#known',
				surfaces: ['settings'],
				reason: 'Allowlisted serious label on settings only for test #10346'
			}
		]
	};
	const findings = flatten('settings', [
		{
			id: 'button-name',
			impact: 'critical',
			help: 'Buttons must have discernible text',
			nodes: [{ target: ['#unlisted'], html: '<button></button>' }]
		},
		{
			id: 'label',
			impact: 'serious',
			help: 'label',
			nodes: [{ target: ['#known'], html: '<input>' }]
		},
		{
			id: 'region',
			impact: 'moderate',
			help: 'region',
			nodes: [{ target: ['#m'], html: '' }]
		},
		{
			id: 'x',
			impact: 'minor',
			help: 'minor',
			nodes: [{ target: ['#n'], html: '' }]
		},
		{
			id: 'y',
			impact: null,
			help: 'null impact',
			nodes: [{ target: ['#o'], html: '' }]
		}
	]);
	const labelFinding = findings.find((f) => f.rule === 'label');
	assert.ok(labelFinding, 'if label finding missing then classify test is inert');
	const wrongSurface = { ...labelFinding, surface: 'performance-more' };
	const mixed = [...findings, wrongSurface];
	const { unexpected, allowed, info } = classify(mixed, allow);
	assert.equal(unexpected.length, 2);
	assert.ok(unexpected.some((f) => f.rule === 'button-name'));
	assert.ok(unexpected.some((f) => f.rule === 'label' && f.surface === 'performance-more'));
	assert.equal(allowed.length, 1);
	assert.equal(allowed[0].rule, 'label');
	assert.equal(info.length, 3);
});

test('staleEntries reports unmatched surfaces and skips optional', () => {
	const allow = {
		disabledRules: [],
		findings: [
			{
				rule: 'a',
				selector: '#b',
				surfaces: ['settings', 'performance-more'],
				reason: 'Two surfaces only one will match in test #10346'
			},
			{
				rule: 'c',
				selector: '#d',
				surfaces: ['library-context-menu'],
				reason: 'Optional volatile entry never stale in test #10346',
				optional: true
			}
		]
	};
	const allowed = [
		{ surface: 'settings', rule: 'a', selector: '#b', impact: 'serious', help: '', html: '' }
	];
	const stale = staleEntries(allow, allowed);
	assert.ok(stale.includes('a | #b | performance-more'));
	assert.ok(!stale.some((s) => s.startsWith('c |')));
});

test('selectorOf flattens shadow chains', () => {
	assert.equal(selectorOf({ target: ['#a'] }), '#a');
	assert.equal(selectorOf({ target: [['#host', '.inner']] }), '#host >>> .inner');
});

test('agent parity: flatten and classify catch planted button-name without UI', () => {
	const violations = [
		{
			id: 'button-name',
			impact: 'critical',
			help: 'Buttons must have discernible text',
			nodes: [
				{
					target: ['#axe-probe', '.axe-probe-btn'],
					html: '<button class="axe-probe-btn"></button>'
				}
			]
		}
	];
	const findings = flatten('settings', violations);
	const { unexpected } = classify(findings, REAL_ALLOW);
	assert.equal(unexpected.length, 1);
	assert.equal(unexpected[0].rule, 'button-name');
	assert.match(unexpected[0].selector, /axe-probe/);
});
