#!/usr/bin/env node
// Story parity: does every React twin render pixel-identical to its Svelte
// original? Reads two STATIC Storybook builds (`pnpm build-storybook` and
// `pnpm build-storybook:react`), pairs stories by id (both stories share one
// literal title and one X.contract.ts), renders each pair in Chromium after
// its play function, and pixel-diffs them.
//
//   node scripts/story-parity.mjs [--svelte storybook-static]
//        [--react storybook-static-react] [--out story-parity] [--max-diff-ratio 0]
//
// Verdicts per story: PASS (differing pixels / all pixels <= --max-diff-ratio), FAIL (more),
// BLANK (both frames one flat color, so nothing was compared; not a pass),
// EMPTY (both blank on a story tagged 'flat-frame' in BOTH builds: its end
// state draws nothing visible, such as a closed dialog, an idle indicator or a
// 0% bar, and its play asserts that state; a tagged story that draws anything
// is a FAIL), MISSING (a Svelte story whose component has an X.tsx twin, but the React
// build lacks the story id), ORPHAN (a React story with no Svelte original).
// BLANK, MISSING and ORPHAN fail the run: an unmeasured twin is never a pass.
// A story whose render phase ends errored or aborted counts as FAIL.
//
// The instrument is checked before any verdict: a story diffed against itself
// must be 0, and two different stories of one component must differ (or, when
// no paired component has two stories, two different stories rendered through
// the same pipeline must differ). If
// either control fails, or no pair was compared, the run is UNKNOWN (exit 2),
// not green. Exit 0 all PASS or EMPTY, 1 any FAIL/BLANK/MISSING/ORPHAN, 2 UNKNOWN.
//
// PW_CHROMIUM_PATH points at a preinstalled Chromium (cloud boxes).

import { createReadStream, existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { createServer } from 'node:http';
import { dirname, extname, isAbsolute, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

import pixelmatch from 'pixelmatch';
import { chromium } from 'playwright';
import { PNG } from 'pngjs';

const FRONTEND = resolve(dirname(fileURLToPath(import.meta.url)), '..');

function arg(name, fallback) {
	const i = process.argv.indexOf(`--${name}`);
	return i >= 0 ? process.argv[i + 1] : fallback;
}

const svelteDir = resolve(FRONTEND, arg('svelte', 'storybook-static'));
const reactDir = resolve(FRONTEND, arg('react', 'storybook-static-react'));
const outDir = resolve(FRONTEND, arg('out', 'story-parity'));
// A share of the frame's pixels, 0 to 1, not a pixel count. The old name
// --max-diff is refused rather than reinterpreted.
if (process.argv.includes('--max-diff')) unknown('--max-diff was renamed --max-diff-ratio (a 0..1 share of pixels)');
const maxDiff = Number(arg('max-diff-ratio', '0'));
if (!Number.isFinite(maxDiff) || maxDiff < 0 || maxDiff > 1) unknown(`--max-diff-ratio must be a number from 0 to 1, got ${arg('max-diff-ratio', '0')}`);

function unknown(msg) {
	console.error(`[UNKNOWN] ${msg}`);
	process.exit(2);
}

function stories(dir) {
	const file = join(dir, 'index.json');
	if (!existsSync(file)) unknown(`${file} not found: build that Storybook first`);
	const entries = Object.values(JSON.parse(readFileSync(file, 'utf8')).entries);
	return new Map(entries.filter((e) => e.type === 'story').map((e) => [e.id, e]));
}

const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.woff2': 'font/woff2', '.png': 'image/png' };

function serve(root) {
	const server = createServer((req, res) => {
		const path = decodeURIComponent(new URL(req.url, 'http://x').pathname);
		let file = resolve(root, `.${path}`);
		// Containment by path relation, not string prefix: a sibling directory
		// whose name starts with the root's would pass startsWith.
		const rel = relative(root, file);
		if (rel === '..' || rel.startsWith(`..${sep}`) || isAbsolute(rel)) return void res.writeHead(403).end();
		if (existsSync(file) && statSync(file).isDirectory()) file = join(file, 'index.html');
		if (!existsSync(file)) return void res.writeHead(404).end();
		res.writeHead(200, { 'content-type': TYPES[extname(file)] ?? 'application/octet-stream' });
		createReadStream(file).pipe(res);
	});
	return new Promise((ok) => server.listen(0, '127.0.0.1', () => ok(server)));
}

async function shot(page, base, id) {
	await page.goto(`${base}/iframe.html?id=${encodeURIComponent(id)}&viewMode=story`);
	// Wait for the render phase to end, which in Storybook 10 is after play.
	await page.waitForFunction(
		() => {
			const phase = window.__STORYBOOK_PREVIEW__?.currentRender?.phase;
			return ['completed', 'finished', 'errored', 'aborted'].includes(phase) || document.body.classList.contains('sb-show-errordisplay');
		},
		null,
		{ timeout: 20000 }
	);
	if (await page.evaluate(() => document.body.classList.contains('sb-show-errordisplay'))) {
		return { error: (await page.locator('#error-message').innerText().catch(() => 'story errored')).trim() };
	}
	// A failed play function can leave the canvas drawn with phase 'errored'
	// (or 'aborted') and no error display; its contract assertions did not
	// hold, so the frame is not evidence of parity.
	const phase = await page.evaluate(() => window.__STORYBOOK_PREVIEW__?.currentRender?.phase);
	if (phase === 'errored' || phase === 'aborted') return { error: `render phase ${phase} (play failed or was cut short)` };
	// Tags as the preview resolved them: a story delegating to X.contract.ts
	// carries its tags there, which the static index.json cannot see.
	const tags = await page.evaluate(() => window.__STORYBOOK_PREVIEW__?.currentRender?.story?.tags ?? null);
	if (!Array.isArray(tags)) return { error: 'the preview exposed no story tags' };
	await page.evaluate(() => document.fonts.ready);
	await page.mouse.move(0, 0);
	await page.waitForTimeout(150);
	// Infinite CSS animations are reset to their start (animations: 'disabled')
	// so a spinner compares by shape, not by when the shot happened.
	// Crop to what the story drew (the union of every visible box under the
	// root, plus a 4px margin), so a small component's diff is a share of
	// its own pixels, not of an 800x400 frame that is mostly empty. Nothing
	// visible (a display:none story) falls back to the whole viewport.
	const clip = await page.evaluate(() => {
		// Settle animations BEFORE measuring: finite ones jump to their end,
		// infinite ones (spinners) reset to their start, so a rotating box
		// does not change the crop from shot to shot.
		for (const a of document.getAnimations()) {
			try {
				a.finish();
			} catch (error) {
				// finish() on an infinite animation throws InvalidStateError;
				// that one resets to the start. Anything else is a real fault.
				if (!(error instanceof DOMException && error.name === 'InvalidStateError')) throw error;
				a.cancel();
			}
		}
		const root = document.querySelector('#storybook-root') ?? document.body;
		let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
		for (const el of root.querySelectorAll('*')) {
			const r = el.getBoundingClientRect();
			if (r.width === 0 || r.height === 0) continue;
			x0 = Math.min(x0, r.left); y0 = Math.min(y0, r.top);
			x1 = Math.max(x1, r.right); y1 = Math.max(y1, r.bottom);
		}
		if (x0 === Infinity) return null;
		const vw = window.innerWidth, vh = window.innerHeight;
		const x = Math.max(0, Math.floor(x0) - 4), y = Math.max(0, Math.floor(y0) - 4);
		return { x, y, width: Math.min(vw, Math.ceil(x1) + 4) - x, height: Math.min(vh, Math.ceil(y1) + 4) - y };
	});
	const used = clip && clip.width > 0 && clip.height > 0 ? clip : null;
	// The crop's origin is returned with the frame: cropping each renderer to
	// its own box normalizes position away, so a twin that moves its box (a
	// dropped margin) would otherwise crop to identical pixels and PASS.
	return { png: PNG.sync.read(await page.screenshot({ animations: 'disabled', ...(used ? { clip: used } : {}) })), box: used, tags };
}

/** Why two crops sit at different places in the 800x400 frame, or null when
 * they share an origin (or neither was cropped). */
function boxMoved(a, b) {
	if (!a && !b) return null;
	const fmt = (box) => (box ? `(${box.x},${box.y})` : 'the whole viewport');
	if (!a || !b || a.x !== b.x || a.y !== b.y) return `drawn box starts at ${fmt(a)} in Svelte but ${fmt(b)} in React`;
	return null;
}

function blank(png) {
	const d = png.data;
	for (let i = 4; i < d.length; i += 4) {
		if (d[i] !== d[0] || d[i + 1] !== d[1] || d[i + 2] !== d[2] || d[i + 3] !== d[3]) return false;
	}
	return true;
}

function diff(a, b) {
	if (a.width !== b.width || a.height !== b.height) return { ratio: 1, png: null };
	const out = new PNG({ width: a.width, height: a.height });
	const n = pixelmatch(a.data, b.data, out.data, a.width, a.height, { threshold: 0.1 });
	return { ratio: n / (a.width * a.height), png: out };
}

const svelte = stories(svelteDir);
const react = stories(reactDir);

const rows = [];
for (const [id, e] of react) if (!svelte.has(id)) rows.push({ id, verdict: 'ORPHAN', note: e.importPath });
const pairs = [];
for (const [id, e] of svelte) {
	const twin = resolve(FRONTEND, e.importPath.replace(/\.stories\.ts$/, '.tsx'));
	const hasTwin = e.importPath.endsWith('.stories.ts') && existsSync(twin);
	if (react.has(id)) pairs.push(id);
	else if (hasTwin) rows.push({ id, verdict: 'MISSING', note: `${twin} exists but its story is not in the React build` });
}

const [sSrv, rSrv] = await Promise.all([serve(svelteDir), serve(reactDir)]);
const sBase = `http://127.0.0.1:${sSrv.address().port}`;
const rBase = `http://127.0.0.1:${rSrv.address().port}`;
// A browser that cannot start measures nothing: UNKNOWN (exit 2), never the
// crash exit 1 that reads like a parity FAIL.
const browser = await chromium
	.launch(process.env.PW_CHROMIUM_PATH ? { executablePath: process.env.PW_CHROMIUM_PATH } : {})
	.catch((error) => {
		sSrv.close();
		rSrv.close();
		unknown(`chromium did not launch: ${error.message.split('\n')[0]}`);
	});
const page = await browser.newPage({ viewport: { width: 800, height: 400 }, deviceScaleFactor: 1 });
mkdirSync(outDir, { recursive: true });

try {
	// Controls, before any verdict. The negative one needs two stories of one
	// component in the Svelte build; without them the instrument is unproven.
	const byComponent = new Map();
	for (const id of svelte.keys()) {
		const key = id.replace(/--[^-]+$/, '');
		byComponent.set(key, [...(byComponent.get(key) ?? []), id]);
	}
	// Try each paired component with two stories until one pair draws
	// differently; stories that close themselves in play can both be blank.
	let controlled = false;
	for (const ids of byComponent.values()) {
		if (ids.length < 2 || !pairs.includes(ids[0])) continue;
		const a = await shot(page, sBase, ids[0]);
		const a2 = await shot(page, sBase, ids[0]);
		const b = await shot(page, sBase, ids[1]);
		if (a.error || a2.error || b.error) continue;
		const self = diff(a.png, a2.png).ratio;
		if (self !== 0) unknown(`positive control: ${ids[0]} differs from itself (non-deterministic render)`);
		const other = diff(a.png, b.png).ratio;
		if (other <= maxDiff) continue;
		console.log(`[control] ${ids[0]} vs itself: 0; vs ${ids[1]}: ${other.toFixed(5)}`);
		controlled = true;
		break;
	}
	// A one-story twin (AlphaBadge, the first) has no second story of its own.
	// Fall back to two DIFFERENT stories (paired first, then any other Svelte
	// story), each rendered and captured
	// through the real pipeline: if navigation or capture handed back the same
	// frame for every URL, they would compare equal and the run is UNKNOWN.
	const drawn = [];
	const others = [...svelte.keys()].filter((id) => !pairs.includes(id));
	for (const id of controlled ? [] : [...pairs, ...others]) {
		const a = await shot(page, sBase, id);
		const a2 = await shot(page, sBase, id);
		if (a.error || a2.error || blank(a.png)) continue;
		if (diff(a.png, a2.png).ratio !== 0) unknown(`positive control: ${id} differs from itself (non-deterministic render)`);
		for (const [other, png] of drawn) {
			const ratio = diff(png, a.png).ratio;
			if (ratio <= maxDiff) continue;
			console.log(`[control] ${id} vs itself: 0; vs ${other}: ${ratio.toFixed(5)}`);
			controlled = true;
			break;
		}
		if (controlled) break;
		drawn.push([id, a.png]);
	}
	if (!controlled) unknown('negative control: no two stories rendered differently through the renderer, so the diff cannot say no');

	for (const id of pairs) {
		const s = await shot(page, sBase, id);
		const r = await shot(page, rBase, id);
		if (s.error || r.error) {
			rows.push({ id, verdict: 'FAIL', note: `story errored: ${s.error ? 'svelte ' + s.error : 'react ' + r.error}` });
			continue;
		}
		const moved = boxMoved(s.box, r.box);
		if (moved) {
			writeFileSync(join(outDir, `${id}.svelte.png`), PNG.sync.write(s.png));
			writeFileSync(join(outDir, `${id}.react.png`), PNG.sync.write(r.png));
			rows.push({ id, verdict: 'FAIL', note: moved });
			continue;
		}
		const d = diff(s.png, r.png);
		const tagged = [s, r].map((x) => x.tags.includes('flat-frame'));
		if (tagged[0] !== tagged[1]) {
			rows.push({ id, verdict: 'FAIL', note: `'flat-frame' tag on the ${tagged[0] ? 'Svelte' : 'React'} story only` });
			continue;
		}
		if (tagged[0]) {
			const drew = [s, r].filter((x) => !blank(x.png)).length;
			rows.push(drew === 0
				? { id, verdict: 'EMPTY', note: "tagged 'flat-frame' and both frames are flat" }
				: { id, verdict: 'FAIL', note: `tagged 'flat-frame' but ${drew === 2 ? 'both builds' : (blank(s.png) ? 'React' : 'Svelte')} drew pixels` });
			continue;
		}
		// Two blank frames match trivially (display:none, or content drawn
		// outside the story root): reported apart, never counted as a PASS.
		if (d.ratio === 0 && blank(s.png)) {
			rows.push({ id, verdict: 'BLANK', note: 'both renders are a single flat color; compared nothing' });
			continue;
		}
		const ok = d.ratio <= maxDiff;
		if (!ok) {
			writeFileSync(join(outDir, `${id}.svelte.png`), PNG.sync.write(s.png));
			writeFileSync(join(outDir, `${id}.react.png`), PNG.sync.write(r.png));
			if (d.png) writeFileSync(join(outDir, `${id}.diff.png`), PNG.sync.write(d.png));
		}
		rows.push({ id, verdict: ok ? 'PASS' : 'FAIL', note: `${(d.ratio * 100).toFixed(3)}% pixels differ` });
	}
} finally {
	await browser.close();
	sSrv.close();
	rSrv.close();
}

rows.sort((x, y) => x.id.localeCompare(y.id));
for (const r of rows) console.log(`[${r.verdict}] ${r.id}: ${r.note}`);
const count = (v) => rows.filter((r) => r.verdict === v).length;
const summary = { pass: count('PASS'), empty: count('EMPTY'), fail: count('FAIL'), blank: count('BLANK'), missing: count('MISSING'), orphan: count('ORPHAN'), svelteOnly: svelte.size - pairs.length - count('MISSING') };
writeFileSync(join(outDir, 'report.json'), JSON.stringify({ summary, rows }, null, '\t'));
console.log(`[summary] ${JSON.stringify(summary)} (svelteOnly = Svelte stories with no twin yet)`);
if (summary.pass + summary.empty + summary.fail === 0) unknown('no story pair was compared');
process.exit(summary.fail + summary.blank + summary.missing + summary.orphan > 0 ? 1 : 0);
