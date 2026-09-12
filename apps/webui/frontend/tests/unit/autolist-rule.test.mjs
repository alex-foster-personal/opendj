import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let rule;

before(async () => {
	rule = await loadTypeScriptModule('src/lib/smartlists/autolist-rule.ts');
});

test('empty selection returns null', () => {
	assert.equal(rule.autolistSelectionToRule({ genre: [], rating: [], bpm: [] }), null);
});

test('genre House matches Python fixture shape', () => {
	const ast = rule.autolistSelectionToRule({ genre: ['House'], rating: [], bpm: [] });
	assert.deepEqual(ast, { field: 'genre', op: '=', value: 'House' });
});

test('within-group union is OR', () => {
	const ast = rule.autolistSelectionToRule({ genre: ['House', 'Techno'], rating: [], bpm: [] });
	assert.equal(ast.op, 'or');
	assert.equal(ast.children.length, 2);
});

test('across-group intersection is AND', () => {
	const ast = rule.autolistSelectionToRule({ genre: ['House'], rating: ['5'], bpm: [] });
	assert.equal(ast.op, 'and');
	assert.equal(ast.children.length, 2);
});

test('summarizeAutolistSelection', () => {
	const title = rule.summarizeAutolistSelection({ genre: ['House'], rating: ['5'], bpm: [] });
	assert.equal(title, 'House, 5 stars');
});
