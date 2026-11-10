// requirement: none (stories + skeleton for story-previews, #10264)
// [if] the stories map is not PERFORMANCE_LAYOUTS.length * 2 named <preset>-1280x800 / <preset>-1920x1080 [then] this fails, [else stop].
// [if] the skeleton's grid-template-areas differs from the spec's areas joined with quotes, or a named area lacks a placeholder [then] this fails, [else stop].
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let layout;
let stories;
let ssr;

before(async () => {
	layout = await loadTypeScriptModule('src/lib/rb/skin-layout.ts');
	stories = await loadTypeScriptModule(
		'src/lib/components/rb/layout/PerfLayoutSkeleton.stories.ts'
	);
	ssr = await loadSvelteSsrModule(
		"export { default as PerfLayoutSkeleton } from '$lib/components/rb/layout/PerfLayoutSkeleton.svelte';\n"
	);
});

test('stories: exactly PERFORMANCE_LAYOUTS.length * 2, named <preset>-1280x800 and <preset>-1920x1080', () => {
	const map = stories.perfLayoutSkeletonStories;
	const expected = layout.PERFORMANCE_LAYOUTS.flatMap((preset) =>
		stories.PERF_LAYOUT_SKELETON_VIEWPORTS.map(({ w, h }) => `${preset}-${w}x${h}`)
	);
	assert.equal(expected.length, layout.PERFORMANCE_LAYOUTS.length * 2);
	assert.deepEqual(Object.keys(map).sort(), [...expected].sort());
	for (const name of expected) {
		assert.equal(map[name].name, name);
		assert.ok(
			Object.values(stories).includes(map[name]),
			`CSF named export missing for story ${name}`
		);
	}
});

test('skeleton: grid-template-areas matches the spec and every area has a placeholder', async () => {
	const { render } = await import('svelte/server');
	for (const preset of layout.PERFORMANCE_LAYOUTS) {
		const spec = layout.PERFORMANCE_LAYOUT_SPEC[preset];
		const areasCss = spec.areas.map((row) => `"${row}"`).join(' ');
		const areasCssHtml = spec.areas.map((row) => `&quot;${row}&quot;`).join(' ');
		const html = render(ssr.PerfLayoutSkeleton, {
			props: { preset, widthPx: 1280, heightPx: 800 }
		}).body;
		assert.match(html, /data-testid="perf-layout-skeleton"/);
		assert.match(html, new RegExp(`data-layout="${preset}"`));
		assert.ok(
			html.includes(areasCss) || html.includes(areasCssHtml),
			`grid-template-areas for ${preset} should be ${areasCss}; got: ${html.slice(0, 500)}`
		);
		const areaNames = [
			...new Set(spec.areas.flatMap((row) => row.trim().split(/\s+/).filter(Boolean)))
		];
		for (const area of areaNames) {
			assert.match(
				html,
				new RegExp(`data-region="${area}"`),
				`preset ${preset} missing placeholder for area ${area}`
			);
		}
		for (const region of ['deck-1', 'deck-2', 'deck-3', 'deck-4', 'mixer', 'browser', 'topbar']) {
			assert.match(
				html,
				new RegExp(`data-region="${region}"`),
				`preset ${preset} missing labelled region ${region}`
			);
		}
		assert.match(html, /wave rows/);
	}
});
