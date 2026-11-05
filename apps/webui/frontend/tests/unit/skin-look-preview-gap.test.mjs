// requirement: PGCL-7693f
/**
 * Preview pins 4dec889975f8, f29c147b1480 and 5fde2c7b4b76 (Preview
 * 5043d179f3, 450a864fc1, 1eb4b57f86): the yellow Free badge is hidden, and
 * on the Gothic skin the jog copies the light wheel and loaded-track titles
 * stay green.
 *
 * Regression lines:
 *  - if the Free badge rule loses display: none then the badge is back
 *  - if the Gothic dial loses its cream plate then the rim disappears into the deck
 *  - if the Gothic loaded-row rule goes then played titles turn gray
 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

const topBar = await readFile('src/lib/components/rb/TopBar.svelte', 'utf8');
const theme = await readFile('src/lib/rb/theme.css', 'utf8');

function rule(css, selector) {
	const at = css.indexOf(`${selector} {`);
	assert.notEqual(at, -1, `${selector} rule missing`);
	return css.slice(at, css.indexOf('}', at));
}

test('the Free badge is hidden', () => {
	assert.match(rule(topBar, '\t.free-badge'), /display: none;/);
});

test('Gothic: the jog carries the light wheel plate and a thick white trail', () => {
	assert.match(
		rule(theme, "html[data-skin='mono-dev'] .perf-root .dial-wrap"),
		/background: #efe9dd;/
	);
	assert.match(
		rule(theme, "html[data-skin='mono-dev'] .perf-root .dial-wrap.dial-playing .progress-trail"),
		/stroke: #fff;/
	);
});

test('Gothic: loaded-track titles stay green', () => {
	assert.match(
		rule(theme, "html[data-skin='mono-dev'] .perf-root .rb-row-loaded"),
		/color: #35c04f;/
	);
});
