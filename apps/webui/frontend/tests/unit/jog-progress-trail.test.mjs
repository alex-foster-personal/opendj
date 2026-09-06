import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { compile } from 'svelte/compiler';

const filename = new URL('../../src/lib/components/rb/deck/JogDial.svelte', import.meta.url);
const source = readFileSync(filename, 'utf8');

test('jog trail starts at the fixed zero tick and follows presented track progress', () => {
	assert.ok(source.includes('class="progress-zero"'));
	assert.ok(source.includes('class="progress-trail"'));
	assert.match(source, /stroke-dasharray=\{`\$\{\(tickAngle \/ 360\) \* dialCircumference\}/);
	assert.match(source, /Math\.min\(1, Math\.max\(0, deck\.position_ms \/ deck\.duration_ms\)\)/);
	compile(source, { filename: filename.pathname, generate: 'client' });
});
