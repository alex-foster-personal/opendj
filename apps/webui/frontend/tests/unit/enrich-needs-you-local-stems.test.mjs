// requirement: PGCL-7693e
/**
 * Preview pin 03e9e56079ae (Preview e107d54cfc): the enrich card's needs-you
 * state is a literal orange on every skin, its hover names what needs a
 * person, and an on-device stems plan is shown as free.
 *
 * Regression lines:
 *  - if the needs-you flag follows a skin token then Gothic grays it out
 *  - if the stems question row is not orange then it reads as background work
 *  - if a local plan quotes GPU minutes or dollars then the prompt misstates the cost
 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const view = await readFile('src/lib/components/rb/EnrichCardView.svelte', 'utf8');
const stems = await readFile('src/lib/rb/stems-jobs.svelte.ts', 'utf8');
const card = await loadTypeScriptModule('src/lib/enrich/enrich-card.ts');

function rule(css, selector) {
	const at = css.indexOf(`${selector} {`);
	assert.notEqual(at, -1, `${selector} rule missing`);
	return css.slice(at, css.indexOf('}', at));
}

test('the needs-you flag reads --rb-warn-text (#10370) and the stems question is a literal orange', () => {
	assert.match(rule(view, '.enrich-flag'), /color: var\(--rb-warn-text, #e8a13a\);/);
	assert.match(rule(view, '.enrich-line.attention'), /color: #e8a13a;/);
	assert.match(view, /class:attention=\{summary\?\.stems\.state === 'ask'\}/);
});

test('the pill hover names what needs a person', () => {
	assert.match(view, /attentionTitle\(summary\)/);
	// enrich-card.test.mjs runs attentionTitle against full summaries.
	assert.equal(typeof card.attentionTitle, 'function');
});

test('a local stems plan is free and quotes no GPU clock', () => {
	assert.match(stems, /export function isLocalStemsPlan\(plan: StemsPlan\): boolean/);
	assert.match(
		stems,
		/if \(isLocalStemsPlan\(plan\)\) return `\$\{counts\}\. Runs on this machine\.`;/
	);
});
