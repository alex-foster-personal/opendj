/**
 * HeadphoneCluster.io-panel.css is pulled into HeadphoneCluster.svelte's
 * <style> with @import, which Svelte does not scope: Vite inlines it as GLOBAL
 * CSS. Same contract as WaveRow.chrome.css: every rule must be rooted at the
 * Audio I/O panel's own .hp-panel[data-audio-io-panel] element.
 *
 * Regression line: if any selector is not rooted at the panel then the file
 * restyles every page that mounts the mixer (a bare `.hp-section button` or
 * `select` would reach other components' controls).
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const dir = new URL('../../src/lib/components/rb/mixer/', import.meta.url);
const css = readFileSync(fileURLToPath(new URL('HeadphoneCluster.io-panel.css', dir)), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
const cluster = readFileSync(fileURLToPath(new URL('HeadphoneCluster.svelte', dir)), 'utf8');
const selectors = [...css.matchAll(/([^{}]+)\{[^}]*\}/g)].flatMap((m) => m[1].split(',').map((s) => s.trim()));

test('the I/O panel stylesheet declares rules and is imported by the cluster', () => {
	assert.ok(selectors.length >= 15, `expected the I/O panel rules, found ${selectors.length}`);
	assert.match(cluster, /<style>\s*@import '\.\/HeadphoneCluster\.io-panel\.css';/);
	assert.match(cluster, /class="hp-panel"[^>]*data-audio-io-panel/);
});

test('every I/O panel selector is rooted at .hp-panel[data-audio-io-panel]', () => {
	const unrooted = selectors.filter((s) => !/^\.hp-panel\[data-audio-io-panel\](\s|$)/.test(s));
	assert.deepEqual(unrooted, [], `unscoped global selectors: ${unrooted.join(' | ')}`);
});
