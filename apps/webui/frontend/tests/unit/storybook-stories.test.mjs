import assert from 'node:assert/strict';
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

// Storybook is the visual decision record, and the thing that makes it worth
// keeping is the scope rule: stories cover PRESENTATIONAL components only,
// props in, nothing global. The house rule forbids mocked APIs, so a story for
// a component that reads a live store is not "a story with a caveat", it is a
// story that lies about what the component does.
//
// PerfMeters.svelte is the named trap. It is the most obvious hover-title
// exemplar in the codebase and it is exactly wrong: four module-level runes
// stores plus a 2s setInterval. It got vetted and rejected once by hand; this
// test is what stops the next person redoing that work, or skipping it.
//
// Regression lines:
// - if a storied component imports a non-const binding from a runes module
//   then Storybook is rendering global state and the scope rule has gone
// - if a storied component grows a setInterval or a fetch then the story is
//   showing something the args do not control
// - if PerfMeters.svelte gets a story then the named trap was walked into
// - if a stories file loses `satisfies Meta<typeof X>` then the Svelte 5 CSF
//   typing gotcha is back (a .svelte default export is a value, not a type)
// - if preview.ts stops adding `perf-root` or importing theme.css then every
//   rb story renders unstyled and wave/render.ts throws on an empty var
// - if preview.css redeclares an --rb-* var then a second palette exists to
//   drift from the real app
// - if main.ts drops the .mdx glob then the ui-decisions docs stop shipping
// - if a decision doc imports from '@storybook/blocks' then it is on the
//   removed pre-v9 path and the page will not build

const SRC = fileURLToPath(new URL('../../src', import.meta.url));
const STORYBOOK = fileURLToPath(new URL('../../.storybook', import.meta.url));
const DECISIONS = join(SRC, 'stories', 'ui-decisions');

/** Reads with CRLF folded to LF so the scans below match on a Windows checkout. */
function read(path) {
	return readFileSync(path, 'utf8').replaceAll('\r\n', '\n');
}

function filesUnder(dir, suffix) {
	const out = [];
	for (const entry of readdirSync(dir)) {
		const path = join(dir, entry);
		if (statSync(path).isDirectory()) out.push(...filesUnder(path, suffix));
		else if (entry.endsWith(suffix)) out.push(path);
	}
	return out;
}

/** POSIX-shaped path relative to src/, so assertion messages read the same everywhere. */
function rel(path) {
	return path.slice(SRC.length + 1).replaceAll('\\', '/');
}

/** The <script> block of a Svelte component, which is where the imports and timers live. */
function scriptOf(source) {
	const start = source.indexOf('<script');
	const open = source.indexOf('>', start);
	const end = source.indexOf('</script>', open);
	assert.ok(start !== -1 && end !== -1, 'component has no <script> block');
	return source.slice(open + 1, end);
}

/**
 * Every `$lib/...` import in a script, as { module, bindings }. Bindings keep
 * the `type ` prefix off; a type import cannot carry state at runtime.
 */
function libImports(script) {
	const out = [];
	for (const match of script.matchAll(/import\s+\{([^}]*)\}\s+from\s+'\$lib\/([^']+)'/g)) {
		const bindings = match[1]
			.split(',')
			.map((entry) => entry.trim())
			.filter((entry) => entry.length > 0 && !entry.startsWith('type '))
			.map((entry) => entry.split(/\s+as\s+/)[0].trim());
		out.push({ module: match[2], bindings });
	}
	return out;
}

/**
 * A `*.svelte.ts` module is a runes module: it may hold module-level $state,
 * and the way that state reaches a component is an exported FUNCTION acting as
 * a read accessor. A story may therefore import the module's `export const`
 * tables and its types, and nothing else. That is exactly the line between
 * AnalysisDots (pulls the ANALYSIS_* tables, still pure props) and PerfMeters
 * (pulls audioHealthHz() and friends, which read live state).
 */
function isConstantExport(moduleSource, binding) {
	return new RegExp(`^export const ${binding}\\b`, 'm').test(moduleSource);
}

const STORY_FILES = filesUnder(SRC, '.stories.ts');

test('there are stories, and each one sits beside the component it documents', () => {
	assert.ok(STORY_FILES.length > 0, 'no *.stories.ts found under src/');
	for (const story of STORY_FILES) {
		const component = story.replace(/\.stories\.ts$/, '.svelte');
		assert.ok(
			existsSync(component),
			`${rel(story)} has no co-located component at ${rel(component)}`
		);
		const name = component.split(/[\\/]/).pop();
		assert.match(
			read(story),
			new RegExp(`from '\\./${name.replace('.', '\\.')}'`),
			`${rel(story)} must import ./${name} relatively so the story and the component move together`
		);
	}
});

test('every storied component is presentational: no timer, no fetch, no global store', () => {
	for (const story of STORY_FILES) {
		const component = story.replace(/\.stories\.ts$/, '.svelte');
		const script = scriptOf(read(component));

		assert.ok(
			!script.includes('setInterval('),
			`${rel(component)} runs a setInterval, so its story would animate on its own rather than from args (this is the PerfMeters shape)`
		);
		assert.ok(
			!/\bfetch\(/.test(script),
			`${rel(component)} fetches, and the house rule forbids mocking it - extract the presentational part and story that instead`
		);

		for (const { module, bindings } of libImports(script)) {
			const runes = join(SRC, 'lib', `${module}.ts`);
			if (!module.endsWith('.svelte') || !existsSync(runes)) continue;
			const moduleSource = read(runes);
			for (const binding of bindings) {
				assert.ok(
					isConstantExport(moduleSource, binding),
					`${rel(component)} imports ${binding} from the runes module $lib/${module}, and it is not an 'export const'. A non-const export of a runes module is a read accessor for module-level state, so a story of this component would render global state rather than its args.`
				);
			}
		}
	}
});

test('PerfMeters keeps its rejection: the obvious exemplar is the wrong one', () => {
	const perfMeters = join(SRC, 'lib', 'components', 'rb', 'PerfMeters.svelte');
	assert.ok(existsSync(perfMeters), 'PerfMeters.svelte moved; re-point this guard');
	assert.ok(
		!existsSync(perfMeters.replace('.svelte', '.stories.ts')),
		'PerfMeters.svelte reads four runes stores and ticks every 2s. It looks like the hover-title exemplar and is not one. QualityBadge and AnalysisDots carry that rule instead.'
	);
});

test('story metadata uses the Svelte 5 CSF typing, not the component as a type', () => {
	for (const story of STORY_FILES) {
		const source = read(story);
		assert.match(
			source,
			/satisfies Meta<typeof \w+>/,
			`${rel(story)} must end its meta with 'satisfies Meta<typeof Component>'; a .svelte default export is a value, so Meta<Component> does not compile`
		);
	}
});

test('preview keeps the perf-root theme scope and does not fork the palette', () => {
	const preview = read(join(STORYBOOK, 'preview.ts'));
	assert.match(
		preview,
		/classList\.add\('perf-root'\)/,
		'preview.ts must put perf-root on <body>: theme.css scopes every --rb-* var under it, and wave/render.ts readPalette throws on an empty var'
	);
	assert.match(preview, /theme\.css'/, 'preview.ts must import the real theme.css');

	const css = read(join(STORYBOOK, 'preview.css'));
	assert.ok(
		!/^\s*--rb-[\w-]+\s*:/m.test(css),
		'preview.css declares an --rb-* var; the palette has exactly one home in theme.css'
	);
});

test('the stories glob picks up both the stories and the decision docs', () => {
	const main = read(join(STORYBOOK, 'main.ts'));
	assert.match(main, /'\.\.\/src\/\*\*\/\*\.mdx'/, 'main.ts must glob ../src/**/*.mdx');
	assert.match(main, /\.stories\.@\(js\|ts\)/, 'main.ts must glob ../src/**/*.stories.@(js|ts)');
	assert.match(main, /framework: '@storybook\/sveltekit'/, 'framework must be @storybook/sveltekit');
});

test('each decision doc is a real page on the current blocks import path', () => {
	const docs = filesUnder(DECISIONS, '.mdx');
	assert.ok(docs.length >= 3, 'expected the template plus the two house rules');
	for (const doc of docs) {
		const source = read(doc);
		assert.match(
			source,
			/from '@storybook\/addon-docs\/blocks'/,
			`${rel(doc)} must import blocks from '@storybook/addon-docs/blocks'; '@storybook/blocks' is the removed pre-v9 path`
		);
		assert.match(source, /<Meta title="/, `${rel(doc)} must declare a sidebar title`);
		if (doc.endsWith('_TEMPLATE.mdx')) continue;
		assert.match(
			source,
			/<Canvas of=\{/,
			`${rel(doc)} states a rule with no live example; a decision doc points at a real story`
		);
	}
});
