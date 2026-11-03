/**
 * QLT letter rendering (QLTSCAN-05): `src/lib/rb/quality-grade.ts`.
 *
 * Regression lines:
 * - if an unchecked grade is drawn solid then it reads as verified: broken
 * - if a grade with issues has no dot then the issue is invisible: broken
 * - if an unchecked summary reads like a measured one then broken
 * - if a null letter renders anything but '?' then a letter was invented: broken
 * - if the meanings drift from apps/shared/audio_grade.py then broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/quality-grade.ts');
});

const grade = (over) => ({ letter: 'B', checked: true, issues: [], ...over });

test('a checked letter is solid, compact shows the letter alone', () => {
	const view = mod.gradeView(grade({}), true);
	assert.equal(view.label, 'B');
	assert.equal(view.classes, 'q-badge q-grade q-grade-B compact');
	assert.equal(view.dataGrade, 'B');
	assert.equal(view.dot, false);
	assert.equal(view.title, 'B -- Good on a club system\nIssues: none found');
});

test('an unchecked letter is hollow and its summary says so', () => {
	const view = mod.gradeView(grade({ checked: false }), true);
	assert.ok(view.classes.split(' ').includes('hollow'));
	assert.equal(view.dataGrade, 'B-unchecked');
	assert.equal(view.title, 'B (not checked yet) -- from bitrate and format only');
});

test('issues put a dot on the badge and are named in the summary', () => {
	const view = mod.gradeView(grade({ issues: ['clipping', 'dc_offset'] }), true);
	assert.equal(view.dot, true);
	assert.match(view.title, /Issues: clipping, dc offset$/);
});

test('full mode shows the letter and its meaning', () => {
	assert.equal(mod.gradeView(grade({}), false).label, 'B -- Good on a club system');
});

test('a null letter renders ? and never invents one', () => {
	const view = mod.gradeView(grade({ letter: null, checked: false }), true);
	assert.equal(view.label, '?');
	assert.ok(view.classes.includes('q-grade-none'));
});

test('the meanings match the Python grade module word for word', () => {
	const py = readFileSync(new URL('../../../../shared/audio_grade.py', import.meta.url), 'utf8');
	for (const [letter, meaning] of Object.entries(mod.GRADE_MEANINGS)) {
		assert.ok(py.includes(`"${letter}": "${meaning}"`), `${letter} meaning drifted`);
	}
});
