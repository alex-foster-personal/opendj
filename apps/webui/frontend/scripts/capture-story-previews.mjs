/**
 * Capture a preview image of EVERY Storybook story in each of the three
 * skins, plus a manifest, so the component gallery artifact can show what
 * each story renders without anyone running Storybook.
 *
 *   pnpm build-storybook
 *   node scripts/capture-story-previews.mjs --out <dir> [--static storybook-static] [--title-prefix Components/Deck]
 *
 * Writes <dir>/<story-id>.<skin>.png and <dir>/manifest.json:
 *   [{ id, title, name, importPath, skins: { default, 'mono-dev', light }, errors: [] }]
 *
 * The story list is read from the BUILT Storybook's index.json, and the skin
 * goes through the preview's `skin` global (`&globals=skin:<skin>`), so the
 * image is what the Storybook toolbar shows. Each image is #storybook-root
 * at 2x, clipped to the union of every visible box under #storybook-root so
 * fixed and absolutely positioned components (menus, modals, banners) are
 * caught too. Exits non-zero when no story matches, a story raises a page
 * error, or a story paints nothing visible without the 'renders-nothing'
 * tag: a blank preview is a failure, never a pass. A box that is present but
 * paints only the background (the clip is non-empty yet every pixel matches
 * the corner pixel) counts as blank too: the capture is decoded in the page and
 * must carry at least MIN_PAINTED_PIXELS pixels that differ from that corner. Every story and skin is
 * attempted, and an exception in one is recorded against that story, so one
 * bad story names itself rather than hiding the rest.
 *
 * Stories tagged 'capture-canvas-width' render into a fixed 600 px canvas
 * instead of shrink-wrapping, for components whose width comes from their
 * container (a fluid bar or list would otherwise capture as a sliver).
 *
 * Only the PNGs this script wrote are ever removed: each run records them in
 * OUT_DIR/.capture-story-previews.json, and the next run deletes exactly those.
 * Any other file in --out is left alone.
 */
import {
	createReadStream,
	existsSync,
	mkdirSync,
	readFileSync,
	rmSync,
	statSync,
	writeFileSync
} from 'node:fs';
import { createServer } from 'node:http';
import { extname, join, resolve, sep } from 'node:path';
import { parseArgs } from 'node:util';

import { chromium } from '@playwright/test';

const { values } = parseArgs({
	options: {
		out: { type: 'string' },
		static: { type: 'string', default: 'storybook-static' },
		'title-prefix': { type: 'string', default: '' }
	}
});
if (!values.out) throw new Error('--out is required');
const STATIC_DIR = resolve(values.static);
const OUT_DIR = resolve(values.out);
if (!existsSync(join(STATIC_DIR, 'index.json'))) {
	throw new Error(`${STATIC_DIR}/index.json not found: run pnpm build-storybook first`);
}

// The skin list is read from the app's own source rather than repeated here,
// so a fourth skin is captured without editing this script. (ui-skin.ts uses
// extensionless imports, which plain node cannot load, hence the read.)
const SKIN_SOURCE = readFileSync(new URL('../src/lib/rb/ui-skin.ts', import.meta.url), 'utf8');
const SKIN_LIST = SKIN_SOURCE.match(/UI_SKIN_CHOICES:[^=]*=\s*\[([^\]]*)\]/);
if (!SKIN_LIST) throw new Error('UI_SKIN_CHOICES not found in src/lib/rb/ui-skin.ts');
const UI_SKIN_CHOICES = [...SKIN_LIST[1].matchAll(/'([^']+)'/g)].map((match) => match[1]);
if (UI_SKIN_CHOICES.length === 0) throw new Error('UI_SKIN_CHOICES parsed as empty');

const MIME = {
	'.html': 'text/html',
	'.js': 'text/javascript',
	'.css': 'text/css',
	'.json': 'application/json',
	'.svg': 'image/svg+xml',
	'.woff2': 'font/woff2',
	'.png': 'image/png'
};

const index = JSON.parse(readFileSync(join(STATIC_DIR, 'index.json'), 'utf8'));
const stories = Object.values(index.entries).filter(
	(entry) => entry.type === 'story' && entry.title.startsWith(values['title-prefix'])
);
if (stories.length === 0) throw new Error(`no story title starts with "${values['title-prefix']}"`);

mkdirSync(OUT_DIR, { recursive: true });
const OWNED_LIST = join(OUT_DIR, '.capture-story-previews.json');
if (existsSync(OWNED_LIST)) {
	for (const name of JSON.parse(readFileSync(OWNED_LIST, 'utf8'))) {
		if (/^[\w.-]+\.png$/.test(name)) rmSync(join(OUT_DIR, name), { force: true });
	}
}
const written = [];

// The server starts only after every argument and story check has passed, so
// a usage error exits without leaving a listening handle open.
const server = createServer((request, response) => {
	let pathname;
	try {
		pathname = decodeURIComponent(new URL(request.url, 'http://x').pathname);
	} catch {
		response.writeHead(400).end();
		return;
	}
	const path = resolve(STATIC_DIR, `.${pathname}`);
	// A bare prefix check lets a sibling such as storybook-static-secret through.
	const inside = path === STATIC_DIR || path.startsWith(STATIC_DIR + sep);
	if (!inside || !existsSync(path) || statSync(path).isDirectory()) {
		response.writeHead(404).end();
		return;
	}
	response.writeHead(200, { 'content-type': MIME[extname(path)] ?? 'application/octet-stream' });
	createReadStream(path).pipe(response);
});
await new Promise((done) => server.listen(0, '127.0.0.1', done));
const origin = `http://127.0.0.1:${server.address().port}`;

let browser;
let page;
let pageErrors = [];
const manifest = [];
let failures = 0;

async function captureSkin(story, skin, row) {
	pageErrors = [];
	await page.goto(`${origin}/iframe.html?id=${story.id}&viewMode=story&globals=skin:${skin}`, {
		waitUntil: 'networkidle'
	});
	const root = page.locator('#storybook-root');
	await root.waitFor({ state: 'attached', timeout: 15_000 });
	// Wait for Storybook's render phase to end, play included, as
	// story-parity does. A failed contract assertion can leave the canvas
	// painted with no pageerror, so the frame is taken only once the phase is
	// terminal, and an errored or aborted render is recorded as a failure.
	await page.waitForFunction(
		() => {
			const phase = window.__STORYBOOK_PREVIEW__?.currentRender?.phase;
			return (
				['completed', 'finished', 'errored', 'aborted'].includes(phase) ||
				document.body.classList.contains('sb-show-errordisplay')
			);
		},
		null,
		{ timeout: 20_000 }
	);
	const renderPhase = await page.evaluate(
		() => window.__STORYBOOK_PREVIEW__?.currentRender?.phase ?? null
	);
	if (
		renderPhase === 'errored' ||
		renderPhase === 'aborted' ||
		(await page.evaluate(() => document.body.classList.contains('sb-show-errordisplay')))
	) {
		pageErrors.push(
			`render phase ${renderPhase ?? 'error display'} (play failed or was cut short)`
		);
	}
	// Tags as the preview resolved them. A story that delegates to X.contract.ts
	// (LibraryLoadIndicator.Idle) carries its tags there, which index.json cannot see.
	const exceptions = await page.evaluate(() => window.__storyExceptions.splice(0));
	for (const exception of exceptions) pageErrors.push(exception);
	const resolvedTags = await page.evaluate(
		() => window.__STORYBOOK_PREVIEW__?.currentRender?.story?.tags ?? null
	);
	if (!Array.isArray(resolvedTags)) pageErrors.push('the preview exposed no story tags');
	const tags = Array.isArray(resolvedTags) ? resolvedTags : [];
	// Let entry transitions and fonts settle so the image is the resting state.
	await page.evaluate(() => document.fonts.ready);
	await page.waitForTimeout(250);
	// Shrink the root to its content: a block root is as wide as the
	// viewport, which turns a 40 px badge into a mostly empty strip. A
	// fluid story gets a fixed canvas instead.
	const fixedCanvas = tags.includes('capture-canvas-width');
	await page.addStyleTag({
		content: fixedCanvas
			? '#storybook-root { display: block; width: 600px; min-width: 1px; }'
			: '#storybook-root { display: inline-block; min-width: 1px; }'
	});
	// Menus, modals and banners are fixed or absolutely positioned, so
	// #storybook-root itself can be 0 px tall while they draw. The clip is
	// the union of every visible box under the root instead.
	const clip = await page.evaluate(() => {
		let box = null;
		const rootEl = document.getElementById('storybook-root');
		for (const element of document.querySelectorAll('#storybook-root, #storybook-root *')) {
			// checkVisibility with opacityProperty also rejects an element under an
			// opacity:0 ancestor, so a transparent tooltip cannot widen the crop or
			// make a wholly transparent story look visible.
			if (
				typeof element.checkVisibility === 'function' &&
				!element.checkVisibility({ opacityProperty: true, visibilityProperty: true })
			)
				continue;
			const style = getComputedStyle(element);
			if (style.visibility === 'hidden' || style.display === 'none') continue;
			// getBoundingClientRect ignores scrollports: a virtual list's spacer
			// reports its whole height (about 200,000 px for 10,000 rows) even
			// though its scroller shows 240 px. Clip each box to every ancestor
			// that clips overflow, up to the root.
			const r = element.getBoundingClientRect();
			const rect = { left: r.left, top: r.top, right: r.right, bottom: r.bottom };
			for (
				let up = element.parentElement;
				up && up !== rootEl.parentElement;
				up = up.parentElement
			) {
				const s = getComputedStyle(up);
				if (s.position === 'fixed') break;
				const a = up.getBoundingClientRect();
				if (s.overflowX !== 'visible') {
					rect.left = Math.max(rect.left, a.left);
					rect.right = Math.min(rect.right, a.right);
				}
				if (s.overflowY !== 'visible') {
					rect.top = Math.max(rect.top, a.top);
					rect.bottom = Math.min(rect.bottom, a.bottom);
				}
			}
			if (rect.right - rect.left < 1 || rect.bottom - rect.top < 1) continue;
			box = box
				? {
						left: Math.min(box.left, rect.left),
						top: Math.min(box.top, rect.top),
						right: Math.max(box.right, rect.right),
						bottom: Math.max(box.bottom, rect.bottom)
					}
				: { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom };
		}
		if (!box) return null;
		const left = Math.max(0, box.left - 4);
		const top = Math.max(0, box.top - 4);
		return { x: left, y: top, width: box.right + 4 - left, height: box.bottom + 4 - top };
	});
	const file = `${story.id}.${skin}.png`;
	const png =
		clip === null ? null : await page.screenshot({ clip, fullPage: true, animations: 'disabled' });
	const painted = png === null ? 0 : await countPaintedPixels(page, png);
	if (png === null || painted < MIN_PAINTED_PIXELS) {
		// A story may show that a state renders nothing ("Closed", "None"),
		// but it has to say so with the renders-nothing tag. An untagged
		// blank is a broken story, not a pass.
		const why =
			png === null
				? 'rendered nothing visible'
				: `painted ${painted} pixels, fewer than ${MIN_PAINTED_PIXELS} off the background`;
		if (!tags.includes('renders-nothing'))
			row.errors.push(
				`${skin}: ${why} (tag the story 'renders-nothing' if that is the state it shows)`
			);
		else row.skins[skin] = null;
	} else {
		writeFileSync(join(OUT_DIR, file), png);
		row.skins[skin] = file;
		written.push(file);
	}
	for (const error of pageErrors) row.errors.push(`${skin}: ${error}`);
}

/** A capture must paint at least this many pixels off the background (2x device pixels). */
const MIN_PAINTED_PIXELS = 16;

/**
 * Decode a captured PNG in the page and count pixels that differ from the
 * corner pixel (the background of the clip). A present-but-empty box is a
 * uniform image and counts zero, so it cannot pass as a painted preview.
 */
async function countPaintedPixels(page, png) {
	return page.evaluate(async (base64) => {
		const bytes = Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
		const bitmap = await createImageBitmap(new Blob([bytes], { type: 'image/png' }));
		const canvas = new OffscreenCanvas(bitmap.width, bitmap.height);
		const ctx = canvas.getContext('2d', { willReadFrequently: true });
		ctx.drawImage(bitmap, 0, 0);
		const { data } = ctx.getImageData(0, 0, bitmap.width, bitmap.height);
		const [r0, g0, b0, a0] = [data[0], data[1], data[2], data[3]];
		let count = 0;
		for (let i = 0; i < data.length; i += 4) {
			const delta =
				Math.abs(data[i] - r0) +
				Math.abs(data[i + 1] - g0) +
				Math.abs(data[i + 2] - b0) +
				Math.abs(data[i + 3] - a0);
			if (delta > 24) count++;
		}
		return count;
	}, png.toString('base64'));
}

try {
	browser = await chromium.launch(
		process.env.PW_CHROMIUM_PATH ? { executablePath: process.env.PW_CHROMIUM_PATH } : {}
	);
	page = await browser.newPage({ viewport: { width: 900, height: 600 }, deviceScaleFactor: 2 });
	page.on('pageerror', (error) => pageErrors.push(String(error)));
	// Storybook 10 ends a story whose play function threw as phase 'finished'
	// with no error display, so the phase alone cannot see it. The channel
	// announces the exception instead; record it before any story mounts.
	await page.addInitScript(() => {
		window.__storyExceptions = [];
		let channel;
		Object.defineProperty(window, '__STORYBOOK_ADDONS_CHANNEL__', {
			configurable: true,
			get: () => channel,
			set: (value) => {
				channel = value;
				for (const name of ['playFunctionThrewException', 'storyThrewException']) {
					value?.on?.(name, (error) =>
						window.__storyExceptions.push(`${name}: ${error?.message ?? error}`)
					);
				}
			}
		});
	});
	for (const story of stories) {
		const row = {
			id: story.id,
			title: story.title,
			name: story.name,
			importPath: story.importPath,
			skins: {},
			errors: []
		};
		for (const skin of UI_SKIN_CHOICES) {
			try {
				await captureSkin(story, skin, row);
			} catch (error) {
				// Record the exception against this story and keep going: one story's
				// navigation or screenshot failure must not end the gallery run.
				row.errors.push(`${skin}: ${String(error?.message ?? error)}`);
			}
		}
		if (row.errors.length > 0) failures += 1;
		console.log(
			`${row.errors.length ? '[FAIL]' : '[OK]'} ${story.id}${row.errors.length ? ` ${row.errors.join(' | ')}` : ''}`
		);
		manifest.push(row);
	}
} finally {
	await browser?.close();
	server.close();
}
writeFileSync(join(OUT_DIR, 'manifest.json'), `${JSON.stringify(manifest, null, 1)}\n`, 'utf8');
writeFileSync(OWNED_LIST, `${JSON.stringify(written, null, 1)}\n`, 'utf8');
console.log(
	`[${failures ? 'FAIL' : 'OK'}] ${stories.length} stories x ${UI_SKIN_CHOICES.length} skins, ${failures} failing, in ${OUT_DIR}`
);
if (failures > 0) process.exit(1);
