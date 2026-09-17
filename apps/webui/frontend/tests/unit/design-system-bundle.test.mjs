/**
 * Design-system bundle build (Claude Design upload layout).
 *
 * Requirements (mini-PRD):
 *   ☑️✅🧪 DS-01 `scripts/build-design-system.mjs` assembles `ds-bundle/` from the shipped
 *      CSS, the wordmark font, the hand-authored cards and the guidelines, deterministically.
 *      [if] the build runs twice and any output byte differs [then ✖︎]
 *      [if] `styles.css` @imports a file that is not on disk [then ✖︎]
 *      [if] a token defined in `app.css` or `theme.css` is absent from `tokens/opendj.tokens.json` [then ✖︎]
 *   ☑️✅🧪 DS-02 every card is registrable by the Claude Design pane.
 *      [if] a card's first line is not `<!-- @dsCard group="..." -->` [then ✖︎]
 *      [if] a card's `<link href>` does not resolve [then ✖︎]
 *      [if] a card has no `.prompt.md` or its first line is blank [then ✖︎]
 *   ☑️✅🧪 DS-03 the bundle carries the app self-check contract.
 *      [if] `_ds_bundle.js` lacks the `@ds-bundle` header or does not parse [then ✖︎]
 *      [if] `.ds-build-meta.json` componentCount != number of cards [then ✖︎]
 *      [if] `README.md` exceeds the 32,000 chars the app inlines [then ✖︎]
 */
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { existsSync, mkdtempSync, readdirSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { after, before, test } from 'node:test';

const FRONTEND = fileURLToPath(new URL('../..', import.meta.url));
const { buildDesignSystem } = await import(
	new URL('../../scripts/build-design-system.mjs', import.meta.url).href
);

let out;
let files;

function walk(dir, prefix = '') {
	const acc = [];
	const entries = readdirSync(dir, { withFileTypes: true }).sort((a, b) => (a.name < b.name ? -1 : 1));
	for (const e of entries) {
		const rel = prefix ? `${prefix}/${e.name}` : e.name;
		if (e.isDirectory()) acc.push(...walk(join(dir, e.name), rel));
		else acc.push(rel);
	}
	return acc;
}
function digestTree(dir) {
	const h = createHash('sha256');
	for (const rel of walk(dir)) {
		h.update(rel);
		h.update(readFileSync(join(dir, rel)));
	}
	return h.digest('hex');
}
function cssCustomProps(css) {
	return [...new Set([...css.matchAll(/(--[\w-]+)\s*:/g)].map((m) => m[1]))];
}
function cardFiles() {
	return files.filter((f) => f.startsWith('components/') && f.endsWith('.html'));
}

before(() => {
	out = mkdtempSync(join(tmpdir(), 'odj-ds-'));
	buildDesignSystem({ frontendDir: FRONTEND, outDir: out });
	files = walk(out);
});
after(() => rmSync(out, { recursive: true, force: true }));

test('DS-01 if the build is not byte-for-byte deterministic across two runs then broken', () => {
	const second = mkdtempSync(join(tmpdir(), 'odj-ds-'));
	try {
		buildDesignSystem({ frontendDir: FRONTEND, outDir: second });
		assert.equal(digestTree(second), digestTree(out));
	} finally {
		rmSync(second, { recursive: true, force: true });
	}
});

test('DS-01 if styles.css does not import every shipped stylesheet, or an import is missing on disk, then broken', () => {
	const styles = readFileSync(join(out, 'styles.css'), 'utf8');
	const imports = [...styles.matchAll(/@import\s+"([^"]+)";/g)].map((m) => m[1]);
	assert.deepEqual(imports, ['./tokens/app.css', './tokens/rb-theme.css', './fonts/fonts.css']);
	for (const rel of imports) assert.ok(existsSync(join(out, rel)), `${rel} missing`);
});

test('DS-01 if the shipped CSS is not copied verbatim then broken', () => {
	assert.equal(
		readFileSync(join(out, 'tokens/app.css'), 'utf8'),
		readFileSync(join(FRONTEND, 'src/app.css'), 'utf8')
	);
	assert.equal(
		readFileSync(join(out, 'tokens/rb-theme.css'), 'utf8'),
		readFileSync(join(FRONTEND, 'src/lib/rb/theme.css'), 'utf8')
	);
});

test('DS-01 if a CSS custom property from app.css or theme.css is absent from the tokens JSON then broken', () => {
	const tokens = JSON.parse(readFileSync(join(out, 'tokens/opendj.tokens.json'), 'utf8'));
	const flat = new Set();
	(function collect(node) {
		for (const [k, v] of Object.entries(node)) {
			if (v && typeof v === 'object' && '$value' in v) flat.add(k);
			else if (v && typeof v === 'object') collect(v);
		}
	})(tokens);
	const appCss = readFileSync(join(FRONTEND, 'src/app.css'), 'utf8');
	const themeCss = readFileSync(join(FRONTEND, 'src/lib/rb/theme.css'), 'utf8');
	const expected = [...cssCustomProps(appCss), ...cssCustomProps(themeCss)];
	// 35 unique names measured Wed 17 Sep 2026 (10 app + 25 perf; the light block reuses the perf names).
	assert.ok(expected.length >= 35, `expected at least the 35 unique token names measured Wed 17 Sep 2026, saw ${expected.length}`);
	for (const name of expected) assert.ok(flat.has(name), `${name} not in tokens json`);
	assert.equal(tokens.perf.light.$extensions.selector, "html[data-theme='light'] .perf-root");
	assert.equal(tokens.perf.light['--rb-bg'].$value, '#f7f3eb');
	assert.equal(tokens.perf.dark['--rb-bg'].$value, '#0d0f12');
	assert.equal(tokens.app['--accent'].$type, 'color');
	assert.equal(tokens.perf.dark['--rb-topbar-h'].$type, 'dimension');
	assert.equal(tokens.perf.dark['--rb-font'].$type, 'fontFamily');
});

test('DS-01 if the wordmark font and its license do not ship with a resolving @font-face then broken', () => {
	const fontsCss = readFileSync(join(out, 'fonts/fonts.css'), 'utf8');
	const url = /url\(["']?\.\/([^"')]+)["']?\)/.exec(fontsCss)?.[1];
	assert.ok(url, 'no url() in fonts.css');
	assert.ok(existsSync(join(out, 'fonts', url)), `${url} missing`);
	assert.ok(existsSync(join(out, 'fonts/Anybody-OFL.txt')));
	assert.match(fontsCss, /font-family:\s*["']Anybody Wordmark["']/);
});

test('DS-02 if any card lacks the @dsCard first line, a resolving <link href>, or a non-blank .prompt.md then broken', () => {
	const cards = cardFiles();
	assert.ok(cards.length >= 12, `expected >= 12 cards, saw ${cards.length}`);
	for (const rel of cards) {
		const txt = readFileSync(join(out, rel), 'utf8');
		assert.match(txt.split('\n', 1)[0], /^<!--\s*@dsCard\s+group="[^"]+"[^>]*-->$/, rel);
		for (const m of txt.matchAll(/<link\b[^>]*\bhref="([^"]+)"/g)) {
			assert.ok(existsSync(resolve(dirname(join(out, rel)), m[1])), `${rel}: ${m[1]}`);
		}
		const prompt = rel.replace(/\.html$/, '.prompt.md');
		assert.ok(existsSync(join(out, prompt)), `${prompt} missing`);
		assert.ok(readFileSync(join(out, prompt), 'utf8').split('\n', 1)[0].trim(), `${prompt} first line blank`);
	}
});

test('DS-02 if a card that uses rb-* performance classes forgets the .perf-root wrapper then broken', () => {
	for (const rel of cardFiles()) {
		const txt = readFileSync(join(out, rel), 'utf8');
		// Class-attribute usage only: prose in token descriptions may mention these names.
		if (/class="[^"]*\brb-(lit-button|knob|fader|star|panel|row-|waverow)/.test(txt)) {
			assert.match(txt, /class="perf-root/, `${rel} uses rb-* classes outside .perf-root`);
		}
	}
});

test('DS-02 if the generated color card does not carry every palette hex from the shipped CSS then broken', () => {
	const card = readFileSync(join(out, 'components/foundations/Colors/Colors.html'), 'utf8');
	for (const hex of ['#0b0d11', '#ffb43a', '#0d0f12', '#2f6fd6', '#f7f3eb', '#175ea8']) {
		assert.ok(card.includes(hex), `${hex} missing from Colors card`);
	}
});

test('DS-03 if _ds_bundle.js lacks a parseable @ds-bundle header or is not valid JS then broken', () => {
	const src = readFileSync(join(out, '_ds_bundle.js'), 'utf8');
	const m = /^\/\* @ds-bundle: (.*) \*\//.exec(src.split('\n', 1)[0]);
	assert.ok(m, 'header missing');
	const meta = JSON.parse(m[1]);
	assert.equal(meta.namespace, 'OpenDJ');
	assert.deepEqual(meta.components, []);
	assert.deepEqual(meta.inlinedExternals, []);
	assert.equal(typeof meta.sourceHashes, 'object');
	assert.doesNotThrow(() => new Function(src));
});

test('DS-03 if .ds-build-meta.json componentCount disagrees with the card count then broken', () => {
	const meta = JSON.parse(readFileSync(join(out, '.ds-build-meta.json'), 'utf8'));
	assert.equal(meta.componentCount, cardFiles().length);
	assert.equal(meta.shape, 'package');
});

test('DS-03 if README.md is missing, over the 32,000-char prompt budget, or silent on .perf-root then broken', () => {
	const readme = readFileSync(join(out, 'README.md'), 'utf8');
	assert.ok(readme.length < 32000, `README is ${readme.length} chars`);
	assert.match(readme, /\.perf-root/);
	assert.match(readme, /not implemented - see PARITY-TODO/);
	assert.match(readme, /--rb-accent/, 'token reference table missing');
});

test('DS-03 if guidelines/ lacks DESIGN.md, the two UI decisions, the wordmark doc and an index naming them then broken', () => {
	const index = readFileSync(join(out, 'guidelines/index.md'), 'utf8');
	for (const name of [
		'DESIGN.md',
		'ui-decisions/controls-without-data-render-inert.md',
		'ui-decisions/numeric-readouts-carry-hover-titles.md',
		'brand/wordmark-font.md'
	]) {
		assert.ok(existsSync(join(out, 'guidelines', name)), `${name} missing`);
		assert.ok(index.includes(name), `index.md does not list ${name}`);
	}
	const decision = readFileSync(
		join(out, 'guidelines/ui-decisions/controls-without-data-render-inert.md'),
		'utf8'
	);
	assert.doesNotMatch(decision, /^import .* from/m, 'mdx import lines leaked into the md');
	assert.doesNotMatch(decision, /<Canvas|<Meta/, 'mdx jsx blocks leaked into the md');
});

test('DS-03 if the build silently tolerates a missing input instead of failing fast then broken', () => {
	const bogus = mkdtempSync(join(tmpdir(), 'odj-ds-bogus-'));
	try {
		assert.throws(() => buildDesignSystem({ frontendDir: bogus, outDir: join(bogus, 'out') }), /src[\\/]app\.css/);
	} finally {
		rmSync(bogus, { recursive: true, force: true });
	}
});
