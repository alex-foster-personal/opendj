/**
 * Capture one zoomed-in image per Storybook story of a component, as the
 * evidence for a state inventory (specs/state-inventories/README.md).
 *
 *   pnpm build-storybook
 *   node scripts/capture-ui-contracts.mjs \
 *     --title "Components/EnrichCardView" --selector '[data-testid="enrich-card"]' \
 *     --out ../../../specs/ui-contracts/enrich-on-open/states
 *
 * Reads the story list from the BUILT Storybook's index.json, so a story that
 * was added is captured and a story that was deleted leaves no stale image
 * (the out directory's png files are replaced as a set). Each image is the
 * element at 2x device scale: 760 px wide for a 380 px card, sharp enough to
 * read every word. Exits non-zero when the title matches no story, a story
 * throws, or the selector is absent: an empty set is a failure, never a pass.
 */
import { createReadStream, existsSync, mkdirSync, readFileSync, readdirSync, rmSync, statSync } from 'node:fs';
import { createServer } from 'node:http';
import { extname, join, resolve } from 'node:path';
import { parseArgs } from 'node:util';

import { chromium } from '@playwright/test';

const { values } = parseArgs({
	options: {
		title: { type: 'string' },
		selector: { type: 'string' },
		out: { type: 'string' },
		static: { type: 'string', default: 'storybook-static' }
	}
});
for (const key of ['title', 'selector', 'out']) {
	if (!values[key]) throw new Error(`--${key} is required`);
}
const STATIC_DIR = resolve(values.static);
const OUT_DIR = resolve(values.out);
if (!existsSync(join(STATIC_DIR, 'index.json'))) {
	throw new Error(`${STATIC_DIR}/index.json not found: run pnpm build-storybook first`);
}

const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.woff2': 'font/woff2' };

const server = createServer((request, response) => {
	const path = join(STATIC_DIR, decodeURIComponent(new URL(request.url, 'http://x').pathname));
	if (!path.startsWith(STATIC_DIR) || !existsSync(path) || statSync(path).isDirectory()) {
		response.writeHead(404).end();
		return;
	}
	response.writeHead(200, { 'content-type': MIME[extname(path)] ?? 'application/octet-stream' });
	createReadStream(path).pipe(response);
});
await new Promise((done) => server.listen(0, '127.0.0.1', done));
const origin = `http://127.0.0.1:${server.address().port}`;

const index = JSON.parse(readFileSync(join(STATIC_DIR, 'index.json'), 'utf8'));
const stories = Object.values(index.entries).filter((entry) => entry.type === 'story' && entry.title === values.title);
if (stories.length === 0) throw new Error(`no story has title ${values.title}`);

mkdirSync(OUT_DIR, { recursive: true });
for (const stale of readdirSync(OUT_DIR).filter((name) => name.endsWith('.png'))) rmSync(join(OUT_DIR, stale));

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 640, height: 480 }, deviceScaleFactor: 2 });
const errors = [];
page.on('pageerror', (error) => errors.push(String(error)));
try {
	for (const story of stories) {
		await page.goto(`${origin}/iframe.html?id=${story.id}&viewMode=story`, { waitUntil: 'networkidle' });
		const element = page.locator(values.selector);
		await element.waitFor({ timeout: 15_000 });
		const name = story.id.split('--')[1];
		await element.screenshot({ path: join(OUT_DIR, `${name}.png`) });
		console.log(`[OK] ${name}.png`);
	}
} finally {
	await browser.close();
	server.close();
}
if (errors.length > 0) throw new Error(`stories raised page errors: ${errors.join('; ')}`);
console.log(`[OK] ${stories.length} state images in ${OUT_DIR}`);
