// requirement STEMPERF-14075: deck stem chips fill with the stem-set's fetch progress (#10331).
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let fill;
let view;

before(async () => {
	fill = await loadTypeScriptModule('src/lib/rb/stem-chip-fill.ts');
	view = await loadTypeScriptModule('src/lib/rb/stem-status.ts');
});

const CONTROLS = ['vocal', 'instrumental', 'drums', 'bass', 'other'];
const stems = (status, load = null) => ({ status, load });
const loading = (phase, progress = null) => stems('loading', { phase, progress, reason: null });

test('fetching 2/4 fills every chip to half', () => {
	/** [if] fetching with files_done 2 of 4 [then] every control reads 0.5, [else stop]. */
	const s = loading('fetching', { files_done: 2, files_total: 4, bytes_done: 10 });
	for (const c of CONTROLS) assert.equal(fill.stemChipFill(s, c), 0.5);
});

test('non-loading states and probing paint no fill', () => {
	/** [if] ready, unavailable, error or probing [then] the fill is null, [else stop]. */
	for (const status of ['ready', 'unavailable', 'error'])
		assert.equal(fill.stemChipFill(stems(status), 'vocal'), null);
	assert.equal(fill.stemChipFill(loading('probing'), 'vocal'), null);
	assert.equal(fill.stemChipFill(loading('waiting'), 'vocal'), null);
});

test('downloading, decoding and switching are indeterminate', () => {
	/** [if] the phase is downloading, decoding or switching [then] the fill is indeterminate, [else stop]. */
	for (const phase of ['downloading', 'decoding', 'switching']) {
		assert.equal(fill.stemChipFill(loading(phase), 'drums'), 'indeterminate');
	}
});

test('zero or missing totals never divide by zero', () => {
	/** [if] files_total is 0 or progress is missing [then] the fill is indeterminate, never NaN, [else stop]. */
	assert.equal(
		fill.stemChipFill(
			loading('fetching', { files_done: 0, files_total: 0, bytes_done: 0 }),
			'bass'
		),
		'indeterminate'
	);
	assert.equal(fill.stemChipFill(loading('fetching', null), 'bass'), 'indeterminate');
});

test('a filled chip stays disabled and data-fill reads the value', () => {
	/** [if] a chip has a fill [then] it stays disabled and data-fill carries the value, [else stop]. */
	assert.equal(fill.stemChipFillKeepsDisabled(0.5), true);
	assert.equal(fill.stemChipFillKeepsDisabled('indeterminate'), true);
	assert.equal(fill.stemChipFillAttr(0.5), '0.5');
	assert.equal(fill.stemChipFillAttr('indeterminate'), 'indeterminate');
	assert.equal(fill.stemChipFillAttr(null), null);
});

test('the fetching status label is unchanged', () => {
	/** [if] fetching 2/4 [then] stemStatusView still labels FETCHING STEMS 2/4 with a hover tip, [else stop]. */
	const s = {
		...loading('fetching', { files_done: 2, files_total: 4, bytes_done: 0 }),
		source: null,
		error: null
	};
	const v = view.stemStatusView(s);
	assert.equal(v.label, 'FETCHING STEMS 2/4');
	assert.ok(v.tip.length > 0);
});
