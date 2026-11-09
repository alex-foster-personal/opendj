/**
 * Inert controls still say what they ARE, and what they WILL do.
 *
 * Pins 552a810ba13b / 79d3616ab3d9 / 4245bc6913c7 / 02c16b7d1211 (the maintainer, Wed 2
 * Sep 2026). The load-bearing sentence is 552a810ba13b's:
 *
 *   "not implemented tooltips should say what it is still, ideally explains
 *    what it is properly still. They're part of roadmapping! We'll be adding
 *    UI ahead of builds all the time as ui-contracts so we can agree the way
 *    it will look and the explainer and animation are also great ways of
 *    ensuring agents are aligned with user."
 *
 * So these are not placeholders, they are UI CONTRACTS: the tooltip is where
 * the maintainer and an agent agree what a control will do before anyone builds it.
 * Fourteen controls in the topbar shared one identical string,
 * "not implemented - see PARITY-TODO", which says only that it is absent -
 * the single least useful thing to say about a control that is on screen.
 *
 * Regression lines:
 * - if two planned controls share an explainer then the tooltip has stopped
 *   identifying which control the pointer is on
 * - if an explainer stops saying the feature is not built then a disabled
 *   control reads as broken instead of planned
 * - if an explainer is only the status, with no description, then it is the
 *   bare PARITY-TODO string again under a new name
 * - if a topbar control is added with no entry then it inherits nothing and
 *   the audit that found these fourteen has to be run by hand again
 * - if grid-flag-dismiss is back in the planned list then the wired button
 *   reads as unbuilt
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, describe, it, test } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;
before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/planned-explainers.ts');
});

describe('planned explainers', () => {
	it('grid-flag-dismiss is built, not planned', () => {
		assert.ok(!('grid-flag-dismiss' in mod.PLANNED_CONTROLS));
	});

	it('describes every planned control, distinctly', () => {
		const { PLANNED_CONTROLS, plannedTitle } = mod;
		const ids = Object.keys(PLANNED_CONTROLS);
		assert.ok(ids.length >= 12, `only ${ids.length} planned controls catalogued`);
		const seen = new Map();
		for (const id of ids) {
			const title = plannedTitle(id);
			assert.ok(!seen.has(title), `${id} and ${seen.get(title)} share an explainer`);
			seen.set(title, id);
		}
	});

	it('says both what it is and that it is not built', () => {
		const { PLANNED_CONTROLS, plannedTitle, NOT_BUILT_MARK } = mod;
		for (const [id, entry] of Object.entries(PLANNED_CONTROLS)) {
			const title = plannedTitle(id);
			assert.ok(title.includes(NOT_BUILT_MARK), `${id} does not say it is unbuilt`);
			// "Name - what it does". Both halves have to be there: a bare name
			// is the PARITY-TODO string with extra steps.
			const [name, ...rest] = entry.split(' - ');
			assert.ok(name.length > 1, `${id} has no name`);
			assert.ok(
				rest.join(' - ').length > 25,
				`${id} has no real description - that is the bare PARITY-TODO string again`
			);
			assert.ok(title.startsWith(name), `${id} does not lead with its own name`);
		}
	});

	it('refuses an unknown id rather than rendering an empty tooltip', () => {
		const { plannedTitle } = mod;
		assert.throws(() => plannedTitle('no-such-control'), /planned-explainers/);
	});

	it('plannedExplainerBullets returns description and not-built line', () => {
		const { plannedExplainerBullets, NOT_BUILT_MARK } = mod;
		const bullets = plannedExplainerBullets('split-view');
		assert.equal(bullets.length, 2);
		assert.match(bullets[0], /browser and the decks/i);
		assert.ok(bullets[1].includes(NOT_BUILT_MARK));
		assert.throws(() => plannedExplainerBullets('missing-id'), /planned-explainers/);
	});

	// The audit half of pin 552a810ba13b: the point was that FOURTEEN controls
	// shared one string, so a test that only checks the catalogue would pass
	// while the topbar still rendered the old constant.
	test('the topbar renders no bare not-implemented string', () => {
		const topbar = readFileSync(
			fileURLToPath(new URL('../../src/lib/components/rb/TopBar.svelte', import.meta.url)),
			'utf8'
		);
		assert.equal(
			/title=\{INERT_TITLE\}/.test(topbar),
			false,
			'a topbar control is back on the shared bare not-implemented string'
		);
		assert.match(topbar, /plannedTitle\(/);
	});

	// TOPVIEW-06 (Fri 9 Oct 2026) built what 5ccbce4970bb held as a contract:
	// the row is a live checkbox now, so it must not keep the not-built line.
	test('two-track AutoPlay is a live checkbox, no longer a planned row', () => {
		const topbar = readFileSync(
			fileURLToPath(new URL('../../src/lib/components/rb/TopBar.svelte', import.meta.url)),
			'utf8'
		);
		assert.doesNotMatch(topbar, /ap-two-track rb-inert/);
		assert.doesNotMatch(topbar, /plannedTitle\('autoplay-two-track'\)/);
		// The checkbox dispatches the typed command; it no longer writes the pref directly.
		assert.match(
			topbar,
			/data-testid="ap-two-track"[\s\S]*?runPerformanceCommandFromUi\(\{\s*type: 'auto_play_two_track',/
		);
		assert.doesNotMatch(topbar, /setAutoPlayTwoDeck\(/);
		assert.equal(mod.PLANNED_CONTROLS['autoplay-two-track'], undefined);
	});

	test('built top-bar controls explain themselves without the not-built line', () => {
		const { BUILT_CONTROLS, builtExplainerBullets, PLANNED_CONTROLS, NOT_BUILT_MARK } = mod;
		for (const id of Object.keys(BUILT_CONTROLS)) {
			assert.equal(PLANNED_CONTROLS[id], undefined, `${id} is both planned and built`);
			const bullets = builtExplainerBullets(id);
			assert.equal(bullets.length, 1);
			assert.ok(bullets[0].length > 25, `${id} has no real description`);
			assert.ok(!bullets[0].includes(NOT_BUILT_MARK), `${id} still says it is unbuilt`);
		}
		assert.throws(() => builtExplainerBullets('no-such-control'), /planned-explainers/);
	});

	test('TOPVIEW-07: the built catalogue holds exactly the controls it says moved (Sol P1 4301475294)', () => {
		// The loop above only walks keys already present, so a moved control
		// missing from the catalogue would pass it; pin the whole set.
		assert.deepEqual(Object.keys(mod.BUILT_CONTROLS).sort(), [
			'4-waveform-view',
			'autoplay-two-track',
			'information',
			'list-view',
			'scope-view-1',
			'scope-view-2'
		]);
		const topbar = readFileSync(
			fileURLToPath(new URL('../../src/lib/components/rb/TopBar.svelte', import.meta.url)),
			'utf8'
		);
		assert.match(topbar, /const TWO_TRACK_AUTOPLAY_TITLE = builtTitle\('autoplay-two-track'\);/);
		assert.match(topbar, /<label class="ap-row" title=\{TWO_TRACK_AUTOPLAY_TITLE\}>\s*<input\s*type="checkbox"\s*data-testid="ap-two-track"/);
		assert.doesNotMatch(topbar, /'Two-track AutoPlay - /, 'no second, hand-copied title in the top bar');
		assert.throws(() => mod.builtTitle('no-such-control'), /planned-explainers/);
	});

	// REQ: LIBUX-17
	test('the browser MASTER dropdown wires the master-dropdown explainer', () => {
		const panel = readFileSync(
			fileURLToPath(new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url)),
			'utf8'
		);
		assert.match(panel, /class="rb-lit-button rb-inert master-dd"/);
		assert.match(panel, /plannedTitle\('master-dropdown'\)/);
	});
});
