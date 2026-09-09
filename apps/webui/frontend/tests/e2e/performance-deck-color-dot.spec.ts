import { expect, test } from '@playwright/test';
import type { APIRequestContext, Page } from '@playwright/test';
import { inflateSync } from 'node:zlib';

/**
 * Pin 54c59dd3f564 (the maintainer, Sun 6 Sep 2026, live on /performance):
 * "color dot should be vertically center aligned with star icons and be 20%
 * smaller."
 *
 * WHY THIS IS A PIXEL TEST AND NOT A BOX TEST. `.deck-rating-row` is already
 * `display: flex; align-items: center`, so the dot's LAYOUT BOX centre and the
 * star buttons' layout box centres were identical on main (both 249.5 at the
 * default 1280x800 deck) -- a box-geometry assertion passes on the broken head
 * and proves nothing. What is off is the INK: `.rb-star` carries
 * `line-height: 1`, and the ☆ glyph's ink is not centred inside that line box,
 * so the stars paint ~0.7px lower than their own box centre while the dot,
 * being a bordered circle, paints exactly on its box centre. Measured on
 * origin/main a4179ee8c: stars ink centroid y=6.17, dot ink centroid y=5.50,
 * delta -0.67px, with the dot also a full 9px tall against an 11px row.
 *
 * So this reads the composited pixels of the row, exactly as the maintainer sees them,
 * and slices them into the star run and the dot run at those two elements'
 * own box edges.
 *
 * TOLERANCE. 0.35px is half the measured main-head defect: it is loose enough
 * that antialiasing noise cannot flip it and tight enough that reverting the
 * fix turns it red (see the revert check in the PR body). It is NOT a
 * font-independent number -- the optical correction it verifies is a property
 * of the ☆ glyph in the app's own `--rb-font` stack, which is a system stack.
 * This suite runs chromium under playwright.performance.config.ts.
 */

const API_BASE = process.env.PERFORMANCE_E2E_API_BASE ?? 'http://127.0.0.1:8686';
const UI_BASE = process.env.PERFORMANCE_E2E_BASE_URL ?? 'http://127.0.0.1:5273';

/** The dot's diameter before this pin: 9px. 20% smaller is 7.2px. */
const DOT_DIAMETER_BEFORE_PX = 9;
const DOT_DIAMETER_PX = DOT_DIAMETER_BEFORE_PX * 0.8;
/** Device-pixel rounding on a 1x screen costs up to a whole CSS pixel edge. */
const DOT_DIAMETER_TOLERANCE_PX = 0.25;
const INK_CENTRE_TOLERANCE_PX = 0.35;

interface Bitmap {
	width: number;
	height: number;
	/** RGBA, 4 bytes per pixel, row-major. */
	data: Buffer;
}

/**
 * Minimal PNG reader for what Playwright's screenshot() emits: 8-bit,
 * non-interlaced, colour type 6 (RGBA) or 2 (RGB). Anything else raises
 * rather than being guessed at, so a future format change is a loud failure
 * and not a silently wrong measurement.
 */
function decodePng(buffer: Buffer): Bitmap {
	expect(buffer.subarray(0, 8).toString('hex'), 'not a PNG').toBe('89504e470d0a1a0a');
	let offset = 8;
	let width = 0;
	let height = 0;
	let channels = 0;
	const idat: Buffer[] = [];
	while (offset < buffer.length) {
		const length = buffer.readUInt32BE(offset);
		const type = buffer.subarray(offset + 4, offset + 8).toString('ascii');
		const body = buffer.subarray(offset + 8, offset + 8 + length);
		if (type === 'IHDR') {
			width = body.readUInt32BE(0);
			height = body.readUInt32BE(4);
			const bitDepth = body.readUInt8(8);
			const colourType = body.readUInt8(9);
			const interlace = body.readUInt8(12);
			expect(bitDepth, 'screenshot bit depth').toBe(8);
			expect(interlace, 'screenshot interlace').toBe(0);
			expect([2, 6], 'screenshot colour type').toContain(colourType);
			channels = colourType === 6 ? 4 : 3;
		} else if (type === 'IDAT') {
			idat.push(Buffer.from(body));
		} else if (type === 'IEND') {
			break;
		}
		offset += 12 + length;
	}
	const raw = inflateSync(Buffer.concat(idat));
	const stride = width * channels;
	const out = Buffer.alloc(width * height * 4);
	const previous = Buffer.alloc(stride);
	const current = Buffer.alloc(stride);
	for (let y = 0; y < height; y += 1) {
		const filter = raw.readUInt8(y * (stride + 1));
		raw.copy(current, 0, y * (stride + 1) + 1, (y + 1) * (stride + 1));
		for (let i = 0; i < stride; i += 1) {
			const a = i >= channels ? current[i - channels] : 0;
			const b = previous[i];
			const c = i >= channels ? previous[i - channels] : 0;
			let value = current[i];
			if (filter === 1) value += a;
			else if (filter === 2) value += b;
			else if (filter === 3) value += (a + b) >> 1;
			else if (filter === 4) {
				const p = a + b - c;
				const pa = Math.abs(p - a);
				const pb = Math.abs(p - b);
				const pc = Math.abs(p - c);
				value += pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
			} else if (filter !== 0) {
				throw new Error(`unsupported PNG filter ${filter} on row ${y}`);
			}
			current[i] = value & 0xff;
		}
		for (let x = 0; x < width; x += 1) {
			out[(y * width + x) * 4] = current[x * channels];
			out[(y * width + x) * 4 + 1] = current[x * channels + 1];
			out[(y * width + x) * 4 + 2] = current[x * channels + 2];
			out[(y * width + x) * 4 + 3] = channels === 4 ? current[x * channels + 3] : 255;
		}
		current.copy(previous);
	}
	return { width, height, data: out };
}

/** Distance of a pixel from the row's background colour, 0..765. */
function inkAt(bitmap: Bitmap, x: number, y: number, background: number[]): number {
	const i = (y * bitmap.width + x) * 4;
	return (
		Math.abs(bitmap.data[i] - background[0]) +
		Math.abs(bitmap.data[i + 1] - background[1]) +
		Math.abs(bitmap.data[i + 2] - background[2])
	);
}

function backgroundOf(bitmap: Bitmap): number[] {
	const tally = new Map<string, number>();
	for (let y = 0; y < bitmap.height; y += 1) {
		for (let x = 0; x < bitmap.width; x += 1) {
			const i = (y * bitmap.width + x) * 4;
			const key = `${bitmap.data[i]},${bitmap.data[i + 1]},${bitmap.data[i + 2]}`;
			tally.set(key, (tally.get(key) ?? 0) + 1);
		}
	}
	const [best] = [...tally.entries()].sort((a, b) => b[1] - a[1]);
	return best[0].split(',').map(Number);
}

/** Ink-weighted vertical centroid of one horizontal slice, in pixels. */
function inkCentroidY(bitmap: Bitmap, x0: number, x1: number, background: number[]): number {
	let weighted = 0;
	let total = 0;
	for (let y = 0; y < bitmap.height; y += 1) {
		let row = 0;
		for (let x = x0; x < x1; x += 1) row += Math.min(inkAt(bitmap, x, y, background), 255);
		weighted += (y + 0.5) * row;
		total += row;
	}
	expect(total, 'slice carries no ink at all').toBeGreaterThan(0);
	return weighted / total;
}

async function _anyOnDiskTrack(request: APIRequestContext): Promise<string> {
	const response = await request.get(`${API_BASE}/api/v1/tracks?limit=50&available=true`);
	expect(response.ok(), 'track listing must succeed').toBeTruthy();
	const payload = (await response.json()) as {
		items: { stable_id: string; file_exists: boolean }[];
	};
	// `available=true` is the backend's own filter; `file_exists` is re-checked
	// here because the two are not the same claim (a track on an unmounted
	// volume is listed as available and is not on disk -- pin 765f848be484).
	const track = payload.items.find((item) => item.file_exists);
	expect(track, 'the fixture library must carry one on-disk track').toBeDefined();
	return track!.stable_id;
}

/** A deck header only renders its rating row once a track is loaded. */
async function _deckOneLoaded(page: Page, request: APIRequestContext) {
	const stableId = await _anyOnDiskTrack(request);
	await page.setViewportSize({ width: 1280, height: 800 });
	// The launch animation paints an opaque overlay over the whole app for a
	// few seconds; a screenshot taken under it is uniformly background and
	// every ink measurement below would silently read zero.
	await page.addInitScript(() => localStorage.setItem('odj.brand-launch.v1', 'complete'));
	await page.goto(`${UI_BASE}/performance`);
	await expect(page.getByLabel('Open DJ launch animation')).toBeHidden({ timeout: 20_000 });
	await page.waitForFunction(() => window.musicDjToolsPerformance !== undefined, null, {
		timeout: 60_000
	});
	await page.evaluate(async (sid) => {
		await window.musicDjToolsPerformance!.dispatch({ type: 'load', deck: 1, stable_id: sid });
	}, stableId);
	const row = page.locator('.deck-rating-row').first();
	await expect(row).toBeVisible({ timeout: 30_000 });
	return row;
}

test('the deck colour dot is 20% smaller than the 9px it shipped at', async ({ page, request }) => {
	const row = await _deckOneLoaded(page, request);
	const dot = await row.locator('.color-dot').boundingBox();
	expect(dot, 'the colour dot must have a real box').not.toBeNull();
	expect(dot!.height).toBeGreaterThan(DOT_DIAMETER_PX - DOT_DIAMETER_TOLERANCE_PX);
	expect(dot!.height).toBeLessThan(DOT_DIAMETER_PX + DOT_DIAMETER_TOLERANCE_PX);
	expect(dot!.width, 'the dot must stay circular').toBeCloseTo(dot!.height, 1);
});

test('the deck colour dot paints vertically centred on the star icons', async ({
	page,
	request
}) => {
	const row = await _deckOneLoaded(page, request);

	// The two slices are taken from the elements' OWN boxes rather than from
	// runs of inked columns. An earlier revision split the row by ink groups
	// and asserted it found exactly six (five stars, then the dot), which
	// quietly depended on the loaded track being UNRATED: RatingStars only
	// triples the inter-glyph gap while `rating` is null or 0, so a rated
	// track drops to a 1px gap where adjacent glyph antialiasing can merge two
	// stars into one group. That would have gone red for a reason with nothing
	// to do with this pin. Boxes cannot merge.
	const rowBox = (await row.boundingBox())!;
	const starsBox = (await row.locator('.rb-stars').boundingBox())!;
	const dotBox = (await row.locator('.color-dot').boundingBox())!;
	const slice = (left: number, width: number): [number, number] => [
		Math.max(0, Math.floor(left - rowBox.x)),
		Math.min(Math.ceil(left - rowBox.x + width), Math.round(rowBox.width))
	];

	const bitmap = decodePng(await row.screenshot());
	const background = backgroundOf(bitmap);
	const [starsFrom, starsTo] = slice(starsBox.x, starsBox.width);
	const [dotFrom, dotTo] = slice(dotBox.x, dotBox.width);
	expect(starsTo, 'the star run must end before the dot begins').toBeLessThanOrEqual(dotFrom);

	const stars = inkCentroidY(bitmap, starsFrom, starsTo, background);
	const dotCentre = inkCentroidY(bitmap, dotFrom, dotTo, background);
	expect(
		Math.abs(dotCentre - stars),
		`dot ink centre ${dotCentre.toFixed(2)}px vs star ink centre ${stars.toFixed(2)}px`
	).toBeLessThanOrEqual(INK_CENTRE_TOLERANCE_PX);
});
