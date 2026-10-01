#!/usr/bin/env node
/**
 * Build the Open DJ design-system bundle for claude.ai/design.
 *
 * Open DJ is Svelte 5, and Claude Design's `/design-sync` converter only consumes
 * React component libraries (its runtime renders `window.<ns>.*` React elements).
 * This script is the sanctioned "off-script layout" instead: it assembles the
 * exact upload layout the Claude Design app self-checks, from the CSS the app
 * actually ships, with zero reimplementation. See docs/decisions/ADR-NEW-design-system-export.md.
 *
 * Requirements (mini-PRD, statuses per file-docstring convention):
 *   ☑️✅🧪 DS-01 deterministic assembly from shipped sources into `ds-bundle/`
 *      - `tokens/app.css`, `tokens/rb-theme.css`: verbatim copies of `src/app.css` and `src/lib/rb/theme.css`
 *      - `tokens/opendj.tokens.json`: every custom property parsed from those files (DTCG-style `$type`/`$value`)
 *      - `fonts/`: the wordmark woff2, its OFL license, and a `fonts.css` @font-face
 *      - `styles.css`: the @import closure rendered designs receive
 *      [if] any input file is missing [then ✖︎] the build throws, it never emits a partial bundle
 *      [if] two runs differ in any byte [then ✖︎]
 *   ☑️✅🧪 DS-02 cards: generated foundations cards (Colors, Typography, LayoutMetrics, Brand) plus the
 *      hand-authored cards under `design-system/cards/`, each with a `<!-- @dsCard group="..." -->` first line
 *      and a `.prompt.md`
 *      [if] a hand-authored card lacks the @dsCard first line [then ✖︎] the build throws naming the file
 *   ☑️✅🧪 DS-03 app self-check contract: `_ds_bundle.js` with the `@ds-bundle` header (empty namespace,
 *      there are no React components), `.ds-build-meta.json`, `README.md` under 32,000 chars, `guidelines/`
 *      [if] README exceeds the budget [then ✖︎] the build throws
 *   ⛔ No `.jsx`/`.d.ts` per card: there is no React API to describe. The agent composes raw HTML with
 *      the documented classes (README).
 *   ⛔ No `_ds_sync.json`: the storybook/package hash recipes need story facts this layout does not have;
 *      the validator treats its absence on an off-script layout as "re-verify everything", which is correct.
 *
 * Usage: node scripts/build-design-system.mjs [--out <dir>]   (default: <repo>/ds-bundle)
 */
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { basename, dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const DEFAULT_FRONTEND = resolve(HERE, '..');
const DEFAULT_OUT = resolve(DEFAULT_FRONTEND, '..', '..', '..', 'ds-bundle');

export const NAMESPACE = 'OpenDJ';
export const README_BUDGET_CHARS = 32000;
const WORDMARK_FAMILY = 'Anybody Wordmark';

const INPUTS = {
	appCss: 'src/app.css',
	themeCss: 'src/lib/rb/theme.css',
	wordmarkWoff2: 'static/fonts/anybody-800-w150-wordmark.woff2',
	wordmarkLicense: 'static/fonts/Anybody-OFL.txt',
	favicon: 'static/favicon.svg',
	readme: 'design-system/README.md',
	designMd: 'design-system/DESIGN.md',
	cardCss: 'design-system/card.css',
	cardsDir: 'design-system/cards',
	uiDecisionsDir: 'src/stories/ui-decisions',
	wordmarkDoc: '../../../docs/brand/wordmark-font/README.md'
};

const GROUPS = {
	foundations: 'Foundations',
	'app-chrome': 'App chrome',
	performance: 'Performance controls'
};

// ---------------------------------------------------------------- helpers

function mustRead(frontendDir, rel, encoding = 'utf8') {
	const abs = resolve(frontendDir, rel);
	if (!existsSync(abs)) throw new Error(`[DS_INPUT_MISSING] ${rel} not found at ${abs}`);
	return readFileSync(abs, encoding);
}
function sha12(buf) {
	return createHash('sha256').update(buf).digest('hex').slice(0, 12);
}
function writeOut(outDir, rel, content) {
	const abs = join(outDir, rel);
	mkdirSync(dirname(abs), { recursive: true });
	writeFileSync(abs, content);
}
function escapeHtml(s) {
	return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// Extract the `{ ... }` body that follows `selector` (first occurrence), brace-balanced.
function blockFor(css, selector) {
	const start = css.indexOf(selector);
	if (start < 0) throw new Error(`[DS_PARSE] selector ${selector} not found`);
	const open = css.indexOf('{', start);
	let depth = 0;
	for (let i = open; i < css.length; i++) {
		if (css[i] === '{') depth++;
		else if (css[i] === '}' && --depth === 0) return css.slice(open + 1, i);
	}
	throw new Error(`[DS_PARSE] unbalanced block for ${selector}`);
}

function inferType(name, value) {
	if (/^#[0-9a-f]{3,8}$/i.test(value) || /^rgba?\(/i.test(value)) return 'color';
	if (/^-?\d+(\.\d+)?(px|rem|em|vh|vw|%)$/.test(value)) return 'dimension';
	if (/font/.test(name) && value.includes(',')) return 'fontFamily';
	return 'string';
}

/** Custom properties declared directly in `selector`'s block: ordered {name, value, description}. */
export function parseCustomProps(css, selector) {
	const raw = blockFor(css, selector);
	const stripped = raw.replace(/\/\*[\s\S]*?\*\//g, '');
	const props = [];
	for (const m of stripped.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
		const name = m[1];
		const value = m[2].replace(/\s+/g, ' ').trim();
		const descRx = new RegExp(`${name}\\s*:[^;]*;[ \\t]*/\\*([\\s\\S]*?)\\*/`);
		const desc = descRx.exec(raw)?.[1].replace(/\s+/g, ' ').trim() ?? null;
		props.push({ name, value, description: desc });
	}
	return props;
}

function tokenGroup(props, extensions) {
	const group = { $extensions: extensions };
	for (const p of props) {
		group[p.name] = { $type: inferType(p.name, p.value), $value: p.value };
		if (p.description) group[p.name].$description = p.description;
	}
	return group;
}

function mdxToMd(mdx) {
	const lines = mdx.split('\n');
	const out = [];
	for (const line of lines) {
		if (/^import\s.+from\s/.test(line)) continue;
		if (/^<Meta\b/.test(line)) continue;
		const canvas = /^<Canvas\s+of=\{([\w.]+)\}\s*\/>/.exec(line);
		if (canvas) {
			out.push(`> Storybook canvas: \`${canvas[1]}\` (the story lives in the Open DJ repo; it is not bundled here).`);
			continue;
		}
		out.push(line);
	}
	return out.join('\n').replace(/^\s*\n+/, '');
}

// ---------------------------------------------------------------- cards

function cardShell({ group, name, subtitle, title, body, extraStyle = '' }) {
	return (
		`<!-- @dsCard group="${escapeHtml(group)}" name="${escapeHtml(name)}" subtitle="${escapeHtml(subtitle)}" -->\n` +
		`<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8" />\n<title>${escapeHtml(title)}</title>\n` +
		`<link rel="stylesheet" href="../../../styles.css" />\n<link rel="stylesheet" href="../../../_preview/card.css" />\n` +
		(extraStyle ? `<style>\n${extraStyle}\n</style>\n` : '') +
		`</head>\n<body>\n${body}\n</body>\n</html>\n`
	);
}

function swatchGrid(props, { onlyColors = true } = {}) {
	const cells = props
		.filter((p) => !onlyColors || inferType(p.name, p.value) === 'color')
		.map(
			(p) =>
				`<div class="ds-swatch"><div class="ds-swatch-chip" style="background:${escapeHtml(p.value)}"></div>` +
				`<code>${escapeHtml(p.name)}</code><span>${escapeHtml(p.value)}</span>` +
				(p.description ? `<small>${escapeHtml(p.description)}</small>` : '') +
				`</div>`
		)
		.join('\n');
	return `<div class="ds-swatch-grid">\n${cells}\n</div>`;
}

function colorsCard(app, perfDark, perfLight) {
	const body =
		`<section class="ds-section"><h2>App chrome palette <code>:root</code> (src/app.css)</h2>${swatchGrid(app)}</section>\n` +
		`<section class="ds-section"><h2>Performance palette, dark <code>.perf-root</code> (src/lib/rb/theme.css)</h2>${swatchGrid(perfDark)}</section>\n` +
		`<section class="ds-section ds-light"><h2>Performance palette, light <code>html[data-theme='light'] .perf-root</code></h2>${swatchGrid(perfLight)}</section>`;
	return cardShell({
		group: GROUPS.foundations,
		name: 'Colors',
		subtitle: 'App palette, performance palette dark and light',
		title: 'Open DJ colors',
		body
	});
}

function typographyCard(app, perfDark) {
	const sizes = perfDark.filter((p) => /^--rb-fs-/.test(p.name));
	const sizeRows = sizes
		.map(
			(p) =>
				`<div class="ds-row"><code>${escapeHtml(p.name)}</code><span style="font-size:var(${p.name})">Peak Hour Mix 128.00 BPM 7A ${escapeHtml(p.value)}</span><small>${escapeHtml(p.description ?? '')}</small></div>`
		)
		.join('\n');
	const font = perfDark.find((p) => p.name === '--rb-font');
	const body =
		`<section class="ds-section"><h2>Wordmark</h2>` +
		`<div class="ds-wordmark">OPEN DJ</div>` +
		`<p class="ds-note">Anybody 800, width 150, ALL CAPS. Shipped as a 1.6 KB subset (glyphs O P E N D J o p e n d j and space) as <code>fonts/anybody-800-w150-wordmark.woff2</code>, family <code>${WORDMARK_FAMILY}</code>. Use it for the wordmark only.</p></section>\n` +
		`<section class="ds-section"><h2>UI face</h2><p class="ds-sample">System stack, no webfont shipped on purpose (offline-first app): <code>${escapeHtml(font?.value ?? '')}</code></p>` +
		`<div class="perf-root ds-perf-frame">${sizeRows}</div>` +
		`<p class="ds-note">The performance UI is dense on purpose (rekordbox parity): 10 to 13 px. App chrome pages (library, settings) use the browser default 16 px base with <code>0.85rem</code> to <code>0.9rem</code> table text.</p></section>`;
	return cardShell({
		group: GROUPS.foundations,
		name: 'Typography',
		subtitle: 'Wordmark, system UI stack, performance type scale',
		title: 'Open DJ typography',
		body
	});
}

function layoutCard(app, perfDark) {
	const metrics = [...perfDark.filter((p) => inferType(p.name, p.value) === 'dimension'), ...app.filter((p) => inferType(p.name, p.value) === 'dimension')];
	const rows = metrics
		.map(
			(p) =>
				`<div class="ds-row"><code>${escapeHtml(p.name)}</code><div class="ds-bar" style="width:min(100%, ${escapeHtml(p.value)})" title="${escapeHtml(p.value)}"></div><span>${escapeHtml(p.value)}</span><small>${escapeHtml(p.description ?? '')}</small></div>`
		)
		.join('\n');
	const body =
		`<section class="ds-section"><h2>Layout metrics</h2><div class="perf-root ds-perf-frame">${rows}</div>` +
		`<p class="ds-note">App shell: <code>.app-shell</code> is a two-column grid, 220 px sidebar + fluid content. Performance app: a fixed 28 px topbar, one 43 px waveform row per deck, decks, then the browser table filling the rest; the browser's bottom bar is inset by <code>--rb-perf-nav-w</code>.</p></section>`;
	return cardShell({
		group: GROUPS.foundations,
		name: 'LayoutMetrics',
		subtitle: 'Bar heights, art size, bottom-bar inset, shell grid',
		title: 'Open DJ layout metrics',
		body
	});
}

function brandCard(faviconSvg) {
	const body =
		`<section class="ds-section"><h2>Mark and wordmark</h2>` +
		`<div class="ds-brand-stage"><div class="ds-brand-mark">${faviconSvg.trim()}</div><div class="ds-wordmark">OPEN DJ</div></div>` +
		`<p class="ds-note">Two-shade terracotta mark (<code>#9c4b34</code> dark half, <code>#D97757</code> light half) on <code>#050505</code>; the launch screen slides the halves together, honoring <code>prefers-reduced-motion</code>. The SVG is the shipped <code>static/favicon.svg</code>.</p></section>`;
	return cardShell({
		group: GROUPS.foundations,
		name: 'Brand',
		subtitle: 'Terracotta two-shade mark, launch treatment',
		title: 'Open DJ brand',
		body
	});
}

const CARD_HEAD_RX = /^<!--\s*@dsCard\s+group="([^"]+)"[^>]*-->/;

function collectHandAuthoredCards(frontendDir) {
	const root = resolve(frontendDir, INPUTS.cardsDir);
	if (!existsSync(root)) throw new Error(`[DS_INPUT_MISSING] ${INPUTS.cardsDir} not found at ${root}`);
	const cards = [];
	for (const groupDir of readdirSync(root, { withFileTypes: true }).sort((a, b) => (a.name < b.name ? -1 : 1))) {
		if (!groupDir.isDirectory() || groupDir.name.startsWith('_')) continue; // _generated/ holds prompts for generated cards
		if (!GROUPS[groupDir.name]) throw new Error(`[DS_CARD_GROUP] unknown group dir ${groupDir.name}; known: ${Object.keys(GROUPS).join(', ')}`);
		const groupPath = join(root, groupDir.name);
		for (const cardDir of readdirSync(groupPath, { withFileTypes: true }).sort((a, b) => (a.name < b.name ? -1 : 1))) {
			if (!cardDir.isDirectory()) continue;
			const name = cardDir.name;
			const html = join(groupPath, name, `${name}.html`);
			const prompt = join(groupPath, name, `${name}.prompt.md`);
			if (!existsSync(html)) throw new Error(`[DS_CARD_MISSING] ${relative(frontendDir, html)}`);
			if (!existsSync(prompt)) throw new Error(`[DS_CARD_MISSING] ${relative(frontendDir, prompt)}`);
			const txt = readFileSync(html, 'utf8');
			const head = CARD_HEAD_RX.exec(txt.split('\n', 1)[0]);
			if (!head) throw new Error(`[DS_CARD_HEAD] ${relative(frontendDir, html)}: first line must be <!-- @dsCard group="..." -->`);
			if (head[1] !== GROUPS[groupDir.name]) {
				throw new Error(`[DS_CARD_GROUP] ${relative(frontendDir, html)}: group "${head[1]}" but dir says "${GROUPS[groupDir.name]}"`);
			}
			const promptTxt = readFileSync(prompt, 'utf8');
			if (!promptTxt.split('\n', 1)[0].trim()) throw new Error(`[DS_PROMPT_EMPTY] ${relative(frontendDir, prompt)}: first line blank`);
			cards.push({ groupSlug: groupDir.name, name, html: txt, prompt: promptTxt });
		}
	}
	return cards;
}

// ---------------------------------------------------------------- README

function tokenTable(title, props) {
	const rows = props
		.map((p) => `| \`${p.name}\` | \`${p.value}\` | ${(p.description ?? '').replace(/\|/g, '\\|')} |`)
		.join('\n');
	return `### ${title}\n\n| Token | Value | Role |\n|---|---|---|\n${rows}\n`;
}

function cardIndex(cards) {
	const byGroup = new Map();
	for (const c of cards) {
		if (!byGroup.has(c.groupSlug)) byGroup.set(c.groupSlug, []);
		byGroup.get(c.groupSlug).push(c);
	}
	let md = '';
	for (const [slug, list] of byGroup) {
		md += `### ${GROUPS[slug]}\n\n`;
		for (const c of list) {
			const summary = c.prompt.split('\n', 1)[0].trim();
			md += `- **${c.name}**: ${summary} (\`components/${slug}/${c.name}/${c.name}.prompt.md\`)\n`;
		}
		md += '\n';
	}
	return md;
}

// ---------------------------------------------------------------- build

export function buildDesignSystem({ frontendDir = DEFAULT_FRONTEND, outDir = DEFAULT_OUT } = {}) {
	// Read every input up front so a missing file fails before anything is written.
	const appCss = mustRead(frontendDir, INPUTS.appCss);
	const themeCss = mustRead(frontendDir, INPUTS.themeCss);
	const woff2 = mustRead(frontendDir, INPUTS.wordmarkWoff2, null);
	const license = mustRead(frontendDir, INPUTS.wordmarkLicense);
	const favicon = mustRead(frontendDir, INPUTS.favicon);
	const readmeSrc = mustRead(frontendDir, INPUTS.readme);
	const designMd = mustRead(frontendDir, INPUTS.designMd);
	const cardCss = mustRead(frontendDir, INPUTS.cardCss);
	const wordmarkDoc = mustRead(frontendDir, INPUTS.wordmarkDoc);
	const uiDecisionsDir = resolve(frontendDir, INPUTS.uiDecisionsDir);
	if (!existsSync(uiDecisionsDir)) throw new Error(`[DS_INPUT_MISSING] ${INPUTS.uiDecisionsDir}`);
	const decisions = readdirSync(uiDecisionsDir)
		.filter((f) => f.endsWith('.mdx') && !f.startsWith('_'))
		.sort()
		.map((f) => ({ name: f.replace(/\.mdx$/, '.md'), md: mdxToMd(readFileSync(join(uiDecisionsDir, f), 'utf8')) }));

	const app = parseCustomProps(appCss, ':root');
	const perfDark = parseCustomProps(themeCss, '.perf-root');
	const perfLight = parseCustomProps(themeCss, "html[data-theme='light'] .perf-root");
	const handCards = collectHandAuthoredCards(frontendDir);

	const generated = [
		{ groupSlug: 'foundations', name: 'Colors', html: colorsCard(app, perfDark, perfLight), prompt: readFileSync(join(frontendDir, INPUTS.cardsDir, '_generated', 'Colors.prompt.md'), 'utf8') },
		{ groupSlug: 'foundations', name: 'Typography', html: typographyCard(app, perfDark), prompt: readFileSync(join(frontendDir, INPUTS.cardsDir, '_generated', 'Typography.prompt.md'), 'utf8') },
		{ groupSlug: 'foundations', name: 'LayoutMetrics', html: layoutCard(app, perfDark), prompt: readFileSync(join(frontendDir, INPUTS.cardsDir, '_generated', 'LayoutMetrics.prompt.md'), 'utf8') },
		{ groupSlug: 'foundations', name: 'Brand', html: brandCard(favicon), prompt: readFileSync(join(frontendDir, INPUTS.cardsDir, '_generated', 'Brand.prompt.md'), 'utf8') }
	];
	const cards = [...generated, ...handCards];

	const tokens = {
		$description: 'Open DJ design tokens, parsed verbatim from the shipped CSS. Names keep their -- prefix so they match the stylesheets one to one.',
		app: tokenGroup(app, { selector: ':root', source: INPUTS.appCss }),
		perf: {
			dark: tokenGroup(perfDark, { selector: '.perf-root', source: INPUTS.themeCss }),
			light: tokenGroup(perfLight, { selector: "html[data-theme='light'] .perf-root", source: INPUTS.themeCss })
		}
	};

	const readme =
		readmeSrc.trimEnd() +
		'\n\n## Token reference (generated from the shipped CSS)\n\n' +
		tokenTable('App chrome `:root` (src/app.css)', app) +
		'\n' +
		tokenTable('Performance `.perf-root`, dark (src/lib/rb/theme.css)', perfDark) +
		'\n' +
		tokenTable("Performance `html[data-theme='light'] .perf-root`", perfLight) +
		'\n## Card index\n\n' +
		cardIndex(cards);
	if (readme.length >= README_BUDGET_CHARS) {
		throw new Error(`[DS_README_BUDGET] README.md is ${readme.length} chars; the app inlines only ${README_BUDGET_CHARS}`);
	}

	const sourceHashes = {
		[INPUTS.appCss]: sha12(appCss),
		[INPUTS.themeCss]: sha12(themeCss),
		[INPUTS.wordmarkWoff2]: sha12(woff2),
		[INPUTS.favicon]: sha12(favicon)
	};
	const header = JSON.stringify({ namespace: NAMESPACE, components: [], sourceHashes, inlinedExternals: [] }).replace(/\*\//g, '*\\/');
	const bundleJs =
		`/* @ds-bundle: ${header} */\n` +
		`// Open DJ ships no React components: the design system is CSS custom properties plus shared\n` +
		`// classes (see README.md). This namespace exists so the app's self-check finds a bundle.\n` +
		`(function () { window.${NAMESPACE} = window.${NAMESPACE} || {}; })();\n`;

	// ---- emit (fresh directory, so removed cards do not linger)
	rmSync(outDir, { recursive: true, force: true });
	mkdirSync(outDir, { recursive: true });
	writeOut(outDir, 'tokens/app.css', appCss);
	writeOut(outDir, 'tokens/rb-theme.css', themeCss);
	writeOut(outDir, 'tokens/opendj.tokens.json', JSON.stringify(tokens, null, 2) + '\n');
	writeOut(outDir, `fonts/${basename(INPUTS.wordmarkWoff2)}`, woff2);
	writeOut(outDir, 'fonts/Anybody-OFL.txt', license);
	writeOut(
		outDir,
		'fonts/fonts.css',
		`/* Open DJ wordmark face. Subset to "OPENDJopendj " only (1.6 KB) so the app stays offline; do not use for body text. */\n` +
			`@font-face {\n  font-family: "${WORDMARK_FAMILY}";\n  font-weight: 800;\n  font-stretch: 150%;\n  font-display: swap;\n` +
			`  src: url("./${basename(INPUTS.wordmarkWoff2)}") format("woff2");\n}\n`
	);
	writeOut(
		outDir,
		'styles.css',
		`/* Open DJ design system: the @import closure every rendered design receives. */\n` +
			`@import "./tokens/app.css";\n@import "./tokens/rb-theme.css";\n@import "./fonts/fonts.css";\n`
	);
	writeOut(outDir, '_preview/card.css', cardCss);
	writeOut(outDir, '_ds_bundle.js', bundleJs);
	writeOut(outDir, 'README.md', readme);
	writeOut(outDir, 'guidelines/DESIGN.md', designMd);
	writeOut(outDir, 'guidelines/brand/wordmark-font.md', wordmarkDoc);
	for (const d of decisions) writeOut(outDir, `guidelines/ui-decisions/${d.name}`, d.md);
	const guidelineList = ['DESIGN.md', 'brand/wordmark-font.md', ...decisions.map((d) => `ui-decisions/${d.name}`)];
	writeOut(
		outDir,
		'guidelines/index.md',
		`# Open DJ design guidelines\n\nRead DESIGN.md first; the UI decisions are house rules every surface follows.\n\n` +
			guidelineList.map((g) => `- ${g}`).join('\n') +
			'\n'
	);
	for (const c of cards) {
		writeOut(outDir, `components/${c.groupSlug}/${c.name}/${c.name}.html`, c.html);
		writeOut(outDir, `components/${c.groupSlug}/${c.name}/${c.name}.prompt.md`, c.prompt);
	}
	writeOut(
		outDir,
		'.ds-build-meta.json',
		JSON.stringify(
			{
				shape: 'package',
				offScript: true,
				generator: 'apps/webui/frontend/scripts/build-design-system.mjs',
				namespace: NAMESPACE,
				componentCount: cards.length,
				tokenCount: app.length + perfDark.length + perfLight.length,
				sourceHashes
			},
			null,
			2
		) + '\n'
	);
	return { outDir, cards: cards.length, tokens: app.length + perfDark.length + perfLight.length, readmeChars: readme.length };
}

// ---------------------------------------------------------------- CLI

const isMain = process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
	const args = process.argv.slice(2);
	const outIdx = args.indexOf('--out');
	const outDir = outIdx >= 0 ? resolve(args[outIdx + 1]) : DEFAULT_OUT;
	const result = buildDesignSystem({ outDir });
	console.error(`[OK] design-system bundle -> ${result.outDir}: ${result.cards} cards, ${result.tokens} tokens, README ${result.readmeChars} chars`);
}
