/**
 * LIBUX-24 / pin 20cce2bab3d8: library row chrome on /performance.
 */
import { expect, test, type APIRequestContext, type Locator, type Page } from '@playwright/test';
import { inflateSync } from 'node:zlib';

const API_BASE = process.env.PERFORMANCE_E2E_API_BASE ?? 'http://127.0.0.1:8686';
const SEPARATOR_RGB = [0x13, 0x15, 0x19];
const SEPARATOR_TOLERANCE = 28;
const SELECTION_PIXEL_DELTA_MIN = 12;

interface Bitmap {
	width: number;
	height: number;
	data: Buffer;
}

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

function rgbaAt(bitmap: Bitmap, x: number, y: number): [number, number, number, number] {
	const i = (y * bitmap.width + x) * 4;
	return [bitmap.data[i], bitmap.data[i + 1], bitmap.data[i + 2], bitmap.data[i + 3]];
}

function colourDistance(a: number[], b: number[]): number {
	return Math.abs(a[0] - b[0]) + Math.abs(a[1] - b[1]) + Math.abs(a[2] - b[2]);
}

async function artworkTrackStableId(request: APIRequestContext): Promise<string> {
	const response = await request.get(`${API_BASE}/api/v1/tracks?limit=50&available=true`);
	expect(response.ok(), 'track listing must succeed').toBeTruthy();
	const payload = (await response.json()) as {
		items: {
			stable_id: string;
			file_exists: boolean;
			artwork_available: boolean | null;
		}[];
	};
	const track = payload.items.find(
		(item) =>
			item.file_exists &&
			item.artwork_available === true &&
			typeof item.stable_id === 'string' &&
			item.stable_id.length > 0
	);
	expect(track, 'fixture must expose one on-disk row with embedded artwork').toBeDefined();
	return track!.stable_id;
}

async function preparePerformancePage(page: Page, stableId: string): Promise<Locator> {
	await page.setViewportSize({ width: 1280, height: 1000 });
	await page.goto('/performance');
	const tableWrap = page.locator('.table-wrap');
	await expect(tableWrap).toBeVisible({ timeout: 60_000 });
	const row = page.locator(`[data-testid="track-row"][data-stable-id="${stableId}"]`);
	await row.scrollIntoViewIfNeeded();
	await expect(row).toBeVisible({ timeout: 60_000 });
	return row;
}

async function assertArtworkLoaded(page: Page, request: APIRequestContext, stableId: string, row: Locator) {
	const artResponse = await request.get(`${API_BASE}/api/v1/tracks/${stableId}/artwork`);
	expect(artResponse.ok(), 'production artwork route must succeed').toBeTruthy();
	const contentType = artResponse.headers()['content-type'] ?? '';
	expect(contentType.startsWith('image/'), 'artwork response must be an image').toBe(true);
	const artImg = row.locator('.c-art img');
	await expect(artImg).toBeVisible();
	await expect(artImg).toHaveClass(/art-loaded/);
	const dims = await artImg.evaluate((img) => {
		const el = img as HTMLImageElement;
		return { w: el.naturalWidth, h: el.naturalHeight };
	});
	expect(dims.w).toBeGreaterThan(0);
	expect(dims.h).toBeGreaterThan(0);
}

async function screenshotElement(locator: Locator): Promise<Bitmap> {
	const buffer = await locator.screenshot();
	return decodePng(buffer);
}

function countOpaquePixelDeltas(before: Bitmap, after: Bitmap, minAlpha = 200): number {
	let changed = 0;
	expect(before.width).toBe(after.width);
	expect(before.height).toBe(after.height);
	for (let y = 0; y < before.height; y += 1) {
		for (let x = 0; x < before.width; x += 1) {
			const a = rgbaAt(before, x, y);
			const b = rgbaAt(after, x, y);
			if (a[3] < minAlpha && b[3] < minAlpha) continue;
			if (colourDistance(a, b) >= SELECTION_PIXEL_DELTA_MIN) changed += 1;
		}
	}
	return changed;
}

test('performance: selected row highlight spans the library table-wrap width', async ({
	page,
	request
}) => {
	const stableId = await artworkTrackStableId(request);
	const row = await preparePerformancePage(page, stableId);
	await assertArtworkLoaded(page, request, stableId, row);
	await row.click();
	await expect(row).toHaveClass(/rb-row-selected/);

	const widths = await page.evaluate(() => {
		const wrap = document.querySelector('.table-wrap');
		const selected = document.querySelector('[data-testid="track-row"].rb-row-selected');
		if (!(wrap instanceof HTMLElement) || !(selected instanceof HTMLElement)) {
			return null;
		}
		return {
			wrap: wrap.clientWidth,
			row: selected.getBoundingClientRect().width
		};
	});
	expect(widths).not.toBeNull();
	expect(widths!.row).toBeGreaterThanOrEqual(widths!.wrap - 24);
});

test('performance: title text is vertically centered in the row and separator spans full row', async ({
	page,
	request
}) => {
	const stableId = await artworkTrackStableId(request);
	const row = await preparePerformancePage(page, stableId);
	await assertArtworkLoaded(page, request, stableId, row);

	const artCell = row.locator('.c-art');
	await page.mouse.move(0, 0);
	const beforeArt = await screenshotElement(artCell);
	await row.click();
	await expect(row).toHaveClass(/rb-row-selected/);
	await page.mouse.move(0, 0);
	const afterArt = await screenshotElement(artCell);
	const changedPixels = countOpaquePixelDeltas(beforeArt, afterArt);
	expect(changedPixels, 'selection must tint opaque artwork pixels').toBeGreaterThan(8);

	const geometry = await page.evaluate(() => {
		const selected = document.querySelector('[data-testid="track-row"].rb-row-selected');
		// The fixture's one artwork row can sort LAST, so it has no next row.
		// The balance is a property of the separator BETWEEN two rows, so
		// measure the one below the selected row when it exists, else the one
		// above it (the previous row's bottom separator), never neither.
		const nextRow = selected?.nextElementSibling;
		const prevRow = selected?.previousElementSibling;
		const titleCell = selected?.querySelector('.c-title');
		const artistCell = selected?.querySelector('.c-artist');
		const title = selected?.querySelector('.title-text');
		const artImg = selected?.querySelector('.c-art img');
		const nextTitle = nextRow?.querySelector('.title-text');
		const prevTitle = prevRow?.querySelector('.title-text');
		if (!(selected instanceof HTMLElement) || !(title instanceof HTMLElement)) {
			return null;
		}
		if (!(titleCell instanceof HTMLElement) || !(artistCell instanceof HTMLElement)) {
			return { error: 'missing title or artist table cell' as const };
		}
		if (!(artImg instanceof HTMLImageElement) || artImg.naturalWidth <= 0) {
			return { error: 'artwork img must be loaded' as const };
		}
		const titleCellDisplay = window.getComputedStyle(titleCell).display;
		const artistCellDisplay = window.getComputedStyle(artistCell).display;
		const rowBox = selected.getBoundingClientRect();
		const titleBox = title.getBoundingClientRect();
		const artBox = artImg.getBoundingClientRect();
		const rowCenterY = (rowBox.top + rowBox.bottom) / 2;
		const titleCenterY = (titleBox.top + titleBox.bottom) / 2;

		const afterStyle = window.getComputedStyle(selected, '::after');
		const afterLeft = parseFloat(afterStyle.left);
		const afterRight = parseFloat(afterStyle.right);
		const afterWidth = parseFloat(afterStyle.width);
		const separatorSpanPx =
			Number.isFinite(afterWidth) && afterWidth > 0
				? afterWidth
				: rowBox.width - afterLeft - afterRight;
		const separatorY = rowBox.bottom - 0.5;

		let separatorBalanceDelta: number | null = null;
		let separatorBalanceAgainst: 'next' | 'previous' | null = null;
		if (nextRow instanceof HTMLElement && nextTitle instanceof HTMLElement) {
			const nextTitleBox = nextTitle.getBoundingClientRect();
			const nextTitleCenterY = (nextTitleBox.top + nextTitleBox.bottom) / 2;
			const gapAbove = separatorY - titleCenterY;
			const gapBelow = nextTitleCenterY - separatorY;
			separatorBalanceDelta = Math.abs(gapAbove - gapBelow);
			separatorBalanceAgainst = 'next';
		} else if (prevRow instanceof HTMLElement && prevTitle instanceof HTMLElement) {
			const prevBox = prevRow.getBoundingClientRect();
			const prevTitleBox = prevTitle.getBoundingClientRect();
			const prevTitleCenterY = (prevTitleBox.top + prevTitleBox.bottom) / 2;
			const prevSeparatorY = prevBox.bottom - 0.5;
			const gapAbove = prevSeparatorY - prevTitleCenterY;
			const gapBelow = titleCenterY - prevSeparatorY;
			separatorBalanceDelta = Math.abs(gapAbove - gapBelow);
			separatorBalanceAgainst = 'previous';
		}

		return {
			titleCellDisplay,
			artistCellDisplay,
			titleCenterDelta: Math.abs(rowCenterY - titleCenterY),
			rowWidth: rowBox.width,
			separatorSpanPx,
			separatorSpanDelta: Math.abs(separatorSpanPx - rowBox.width),
			separatorBalanceDelta,
			separatorBalanceAgainst,
			artWidth: artBox.width,
			artLeft: artBox.left,
			titleLeft: titleBox.left,
			rowBottom: rowBox.bottom
		};
	});
	expect(geometry).not.toBeNull();
	if (geometry && 'error' in geometry) {
		throw new Error(`row chrome precondition failed: ${geometry.error}`);
	}
	expect(geometry!.titleCellDisplay).toBe('table-cell');
	expect(geometry!.artistCellDisplay).toBe('table-cell');
	expect(geometry!.titleCenterDelta).toBeLessThan(4);
	expect(geometry!.rowWidth).toBeGreaterThan(200);
	expect(geometry!.artWidth).toBeGreaterThan(4);
	expect(geometry!.separatorSpanDelta).toBeLessThan(2);
	expect(
		geometry!.separatorBalanceAgainst,
		'the selected row must have a neighbor row to measure separator balance against'
	).not.toBeNull();
	expect(geometry!.separatorBalanceDelta).not.toBeNull();
	expect(geometry!.separatorBalanceDelta!).toBeLessThanOrEqual(1);

	const rowShot = await screenshotElement(row);
	const probe = await page.evaluate(() => {
		const selected = document.querySelector('[data-testid="track-row"].rb-row-selected');
		const art = selected?.querySelector('.c-art');
		const title = selected?.querySelector('.title-text');
		if (!(selected instanceof HTMLElement) || !(art instanceof HTMLElement) || !(title instanceof HTMLElement)) {
			return null;
		}
		const rowBox = selected.getBoundingClientRect();
		const artBox = art.getBoundingClientRect();
		const titleBox = title.getBoundingClientRect();
		const sepY = rowBox.bottom - 0.5;
		return {
			artLocalX: artBox.left - rowBox.left + artBox.width / 2,
			titleLocalX: titleBox.left - rowBox.left + titleBox.width / 2,
			sepLocalY: sepY - rowBox.top
		};
	});
	expect(probe).not.toBeNull();
	const artX = Math.min(
		rowShot.width - 1,
		Math.max(0, Math.round(probe!.artLocalX))
	);
	const titleX = Math.min(
		rowShot.width - 1,
		Math.max(0, Math.round(probe!.titleLocalX))
	);
	const sampleY = Math.min(
		rowShot.height - 1,
		Math.max(0, Math.round(probe!.sepLocalY))
	);
	const artPixel = rgbaAt(rowShot, artX, sampleY);
	const titlePixel = rgbaAt(rowShot, titleX, sampleY);
	expect(
		colourDistance(artPixel, SEPARATOR_RGB),
		`separator must paint over the artwork column, got rgba(${artPixel.join(',')})`
	).toBeLessThanOrEqual(SEPARATOR_TOLERANCE);
	expect(
		colourDistance(titlePixel, SEPARATOR_RGB),
		`separator must paint under the title column, got rgba(${titlePixel.join(',')})`
	).toBeLessThanOrEqual(SEPARATOR_TOLERANCE);
	// Negative control on the same shot, one pixel above the separator line.
	// The fixture artwork's bottom rows are near-black (#0d0f12), within
	// SEPARATOR_TOLERANCE of the separator colour, so an artwork image painted
	// OVER the separator would also pass the sample above. Directly above the
	// line the selected row must show its highlight instead - tinted artwork
	// in the art column, the selection fill under the title - and neither may
	// read as separator. This proves the probe can say no at both x positions
	// and that the selection highlight reaches the art column's last rows.
	expect(sampleY, 'row screenshot too short for an above-separator control').toBeGreaterThan(0);
	const artAbove = rgbaAt(rowShot, artX, sampleY - 1);
	const titleAbove = rgbaAt(rowShot, titleX, sampleY - 1);
	expect(
		colourDistance(artAbove, SEPARATOR_RGB),
		`selected artwork just above the separator must be tinted, got rgba(${artAbove.join(',')})`
	).toBeGreaterThan(SEPARATOR_TOLERANCE);
	expect(
		colourDistance(titleAbove, SEPARATOR_RGB),
		`selected title cell just above the separator must show the highlight, got rgba(${titleAbove.join(',')})`
	).toBeGreaterThan(SEPARATOR_TOLERANCE);
});
