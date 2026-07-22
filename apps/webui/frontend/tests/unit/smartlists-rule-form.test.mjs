import assert from 'node:assert/strict';
import { before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let ruleForm;

before(async () => {
	ruleForm = await loadTypeScriptModule('src/lib/smartlists/rule-form.ts');
});

beforeEach(() => {
	ruleForm._resetIdCounterForTests();
});

test('astToForm/formToAst round-trips a nested AND/OR/NOT tree unchanged', () => {
	const rule = {
		op: 'and',
		children: [
			{ field: 'bpm', op: 'between', value: [120, 130] },
			{
				op: 'or',
				children: [
					{ field: 'genre', op: '=', value: 'techno' },
					{ field: 'genre', op: 'in', value: ['house', 'electro'] }
				]
			},
			{
				op: 'not',
				children: [{ field: 'color_tag', op: '=', value: 'red' }]
			}
		]
	};

	const form = ruleForm.astToForm(rule);
	assert.deepEqual(ruleForm.formToAst(form), rule);
});

test('round-trips every field type with its representative op', () => {
	const rule = {
		op: 'and',
		children: [
			{ field: 'rating', op: '>=', value: 3 },
			{ field: 'energy', op: 'between', value: [0.2, 0.8] },
			{ field: 'key', op: 'contains', value: 'Am' },
			{ field: 'custom_tags', op: 'contains', value: 'peak-time' },
			{ field: 'custom_tags', op: 'in', value: ['peak-time', 'warmup'] },
			{ field: 'paired_with', op: 'in', value: ['abc123', 'def456'] },
			{ field: 'added_date', op: '>', value: '2026-01-01T00:00:00+00:00' },
			{ field: 'last_played', op: '<=', value: { $relative: '-7d' } }
		]
	};

	const form = ruleForm.astToForm(rule);
	assert.deepEqual(ruleForm.formToAst(form), rule);
});

test('round-trips arbitrary in strings without splitting, trimming, or dropping values', () => {
	const rule = { field: 'genre', op: 'in', value: ['drum, bass', ' drum, bass ', '', 'techno'] };
	const form = ruleForm.astToForm(rule);
	assert.deepEqual(form.valueList, ['drum, bass', ' drum, bass ', '', 'techno']);
	assert.deepEqual(ruleForm.formToAst(form), rule);
});

test('rejects changing a multi-child group to NOT without discarding a child', () => {
	const group = ruleForm.createEmptyGroup('and');
	group.children.push(ruleForm.createEmptyPredicate('genre'));
	group.children.push(ruleForm.createEmptyPredicate('bpm'));

	assert.match(ruleForm.changeGroupOp(group, 'not'), /exactly one child/);
	assert.equal(group.op, 'and');
	assert.equal(group.children.length, 2);
});

test('a bare predicate root (no logical wrapper) round-trips too', () => {
	const rule = { field: 'genre', op: '=', value: 'techno' };
	const form = ruleForm.astToForm(rule);
	assert.equal(form.kind, 'predicate');
	assert.deepEqual(ruleForm.formToAst(form), rule);
});

test('createEmptyPredicate defaults to the first allowed op for its field', () => {
	const predicate = ruleForm.createEmptyPredicate('bpm');
	assert.equal(predicate.field, 'bpm');
	assert.equal(predicate.op, '=');
});

test('createEmptyGroup seeds a NOT group with exactly one child predicate', () => {
	const notGroup = ruleForm.createEmptyGroup('not');
	assert.equal(notGroup.children.length, 1);
	const andGroup = ruleForm.createEmptyGroup('and');
	assert.equal(andGroup.children.length, 0);
});

test('validateForm rejects an op not allowed for the field, mirroring schema.py', () => {
	const form = ruleForm.createEmptyPredicate('genre');
	form.op = 'between'; // genre only allows =, !=, contains, in
	form.valueLo = 'a';
	form.valueHi = 'z';
	const errors = ruleForm.validateForm(form);
	assert.equal(errors.length, 1);
	assert.match(errors[0].message, /not allowed for field "genre"/);
});

test('validateForm requires both between bounds to be present and numeric', () => {
	const form = ruleForm.createEmptyPredicate('bpm');
	form.op = 'between';
	form.valueLo = '120';
	form.valueHi = '';
	let errors = ruleForm.validateForm(form);
	assert.equal(errors.length, 1);
	assert.match(errors[0].message, /value must not be empty/);

	form.valueHi = 'not-a-number';
	errors = ruleForm.validateForm(form);
	assert.equal(errors.length, 1);
	assert.match(errors[0].message, /expected a number/);
});

test('validateForm requires at least one item for an "in" op', () => {
	const form = ruleForm.createEmptyPredicate('genre');
	form.op = 'in';
	form.valueList = [];
	const errors = ruleForm.validateForm(form);
	assert.equal(errors.length, 1);
	assert.match(errors[0].message, /at least one value/);
});

test('validateForm accepts a relative date literal and rejects a garbage one', () => {
	const form = ruleForm.createEmptyPredicate('last_played');
	form.op = '<=';
	form.value = '-7d';
	assert.deepEqual(ruleForm.validateForm(form), []);

	form.value = 'not-a-date';
	const errors = ruleForm.validateForm(form);
	assert.equal(errors.length, 1);
	assert.match(errors[0].message, /ISO-8601 date or a relative literal/);
});

test('validateNodeOwn flags empty AND/OR groups and mis-sized NOT groups (own arity only)', () => {
	const emptyAnd = ruleForm.createEmptyGroup('and');
	assert.deepEqual(ruleForm.validateNodeOwn(emptyAnd), [
		{ path: 'root.children', message: "logical op 'and' requires at least one child" }
	]);

	const notGroup = ruleForm.createEmptyGroup('not');
	notGroup.children.push(ruleForm.createEmptyPredicate('bpm'));
	const errors = ruleForm.validateNodeOwn(notGroup);
	assert.equal(errors.length, 1);
	assert.match(errors[0].message, /'not' requires exactly one child/);
});

test('validateForm recurses into nested children and reports every error with its path', () => {
	const group = ruleForm.createEmptyGroup('and');
	const badPredicate = ruleForm.createEmptyPredicate('bpm');
	badPredicate.value = '';
	group.children.push(badPredicate);
	const nestedGroup = ruleForm.createEmptyGroup('or');
	group.children.push(nestedGroup);

	const errors = ruleForm.validateForm(group);
	assert.equal(errors.length, 2);
	assert.equal(errors[0].path, 'root.children[0].value');
	assert.equal(errors[1].path, 'root.children[1].children');
});

test('formToAst throws with a dotted path on the first unparseable value', () => {
	const form = ruleForm.createEmptyPredicate('rating');
	form.value = 'nope';
	assert.throws(() => ruleForm.formToAst(form), /root\.value: expected a number/);
});

test('formToAst throws for an empty logical group and for an oversized NOT', () => {
	const emptyGroup = ruleForm.createEmptyGroup('or');
	assert.throws(() => ruleForm.formToAst(emptyGroup), /requires at least one child/);

	const notGroup = ruleForm.createEmptyGroup('not');
	notGroup.children.push(ruleForm.createEmptyPredicate('bpm'));
	assert.throws(() => ruleForm.formToAst(notGroup), /'not' requires exactly one child/);
});

test('isFormGroup narrows group vs predicate nodes', () => {
	assert.equal(ruleForm.isFormGroup(ruleForm.createEmptyGroup('and')), true);
	assert.equal(ruleForm.isFormGroup(ruleForm.createEmptyPredicate('bpm')), false);
});
