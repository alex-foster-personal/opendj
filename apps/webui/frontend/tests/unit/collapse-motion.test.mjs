import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * Collapse / expand rule (the maintainer, Tue 27 Oct 2026): 100 ms, transform and
 * opacity or a grid-rows 0fr -> 1fr wrapper only, snap above 200 rows or
 * under prefers-reduced-motion.
 *
 * [if] a block of 201 rows animates [then] long playlists stutter on open - broken.
 * [if] reduced motion still animates [then] the OS setting is ignored - broken.
 * [if] a frame sets height, top or margin [then] rows re-lay-out per frame - broken.
 * [if] the Playlists folder opens with a plain {#if} [then] the rule is not applied - broken.
 */

const FRONTEND = fileURLToPath(new URL('../../', import.meta.url));
let motion;

before(async () => {
	motion = await loadTypeScriptModule('src/lib/motion/collapse.ts');
});

describe('collapseDuration', () => {
	it('animates 100 ms up to and including 200 rows', () => {
		assert.equal(motion.COLLAPSE_MS, 100);
		assert.equal(motion.collapseDuration(0, false), 100);
		assert.equal(motion.collapseDuration(200, false), 100);
	});

	it('snaps above 200 rows', () => {
		assert.equal(motion.collapseDuration(201, false), 0);
		assert.equal(motion.collapseDuration(5000, false), 0);
	});

	it('snaps under prefers-reduced-motion at any size', () => {
		assert.equal(motion.collapseDuration(1, true), 0);
	});

	it('refuses a row count that is not a non-negative integer', () => {
		assert.throws(() => motion.collapseDuration(-1, false), RangeError);
		assert.throws(() => motion.collapseDuration(1.5, false), RangeError);
		assert.throws(() => motion.collapseDuration(Number.NaN, false), RangeError);
	});
});

describe('collapseRowCount', () => {
	const block = (rows) => ({ children: [{ childElementCount: rows }] });

	it('counts every row rendered in the clipping block, hints and notices included', () => {
		assert.equal(motion.collapseRowCount(block(201)), 201);
		assert.equal(motion.collapseDuration(motion.collapseRowCount(block(201)), false), 0);
	});

	it('refuses an element without exactly one clipping child', () => {
		assert.throws(() => motion.collapseRowCount({ children: [] }), /exactly one clipping child/);
		assert.throws(
			() => motion.collapseRowCount({ children: [{ childElementCount: 1 }, { childElementCount: 1 }] }),
			/exactly one clipping child/
		);
	});
});

describe('collapseCss', () => {
	it('sets only grid-template-rows and opacity', () => {
		for (const t of [0, 0.5, 1]) {
			const props = motion
				.collapseCss(t)
				.split(';')
				.map((decl) => decl.split(':')[0].trim())
				.filter(Boolean);
			assert.deepEqual(props.sort(), ['grid-template-rows', 'opacity']);
		}
	});

	it('runs from 0fr shut to 1fr open', () => {
		assert.match(motion.collapseCss(0), /grid-template-rows: 0fr; opacity: 0;/);
		assert.match(motion.collapseCss(1), /grid-template-rows: 1fr; opacity: 1;/);
	});
});

describe('library components follow the rule', () => {
	it('the Playlists folder collapses through the shared transition', () => {
		// The tree is a container plus a presentational view; the markup lives in the view.
		const view = readFileSync(
			join(FRONTEND, 'src/lib/components/rb/browser/PlaylistTreeView.svelte'),
			'utf8'
		);
		const tree =
			readFileSync(join(FRONTEND, 'src/lib/components/rb/browser/PlaylistTree.svelte'), 'utf8') + view;
		assert.match(tree, /<div class="collapse" in:collapse out:collapse>\s*<div class="collapse-inner">/);
		// [if] the block used transition:collapse [then] Svelte would keep the open-time
		// snap decision for the close [else stop] (sol review P1, PR #7600)
		assert.doesNotMatch(view, /transition:collapse/);
		assert.match(view, /from '\$lib\/motion\/collapse'/);
	});

	it('RecentlyDeletedFolderView collapses through the shared transition', () => {
		const view = readFileSync(
			join(FRONTEND, 'src/lib/components/rb/browser/RecentlyDeletedFolderView.svelte'),
			'utf8'
		);
		assert.match(view, /<div class="collapse" in:collapse out:collapse>\s*<div class="collapse-inner">/);
		assert.doesNotMatch(view, /transition:collapse/);
		assert.match(view, /from '\$lib\/motion\/collapse'/);
	});

	it('no library browser component uses svelte slide, which animates height', () => {
		const dir = join(FRONTEND, 'src/lib/components/rb/browser');
		const offenders = readdirSync(dir)
			.filter((name) => name.endsWith('.svelte'))
			.filter((name) => /\bslide\b[^;]*from 'svelte\/transition'/.test(readFileSync(join(dir, name), 'utf8')));
		assert.deepEqual(offenders, []);
	});
});
