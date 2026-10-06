// SET OUTPUTS sits 10px clear of the VOL knob (the maintainer, Mon 5 Oct 2026: "it's too
// close to VOL").
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const src = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/mixer/HeadphoneCluster.svelte', import.meta.url)),
	'utf8'
);

test('SET OUTPUTS button keeps a 10px gap from VOL', () => {
	const rule = /\n\t\.hp-btn-io \{([^}]*)\}/.exec(src);
	assert.ok(rule, 'control: the .hp-btn-io rule is found');
	assert.match(rule[1], /margin-left:\s*10px;/);
});
