#!/usr/bin/env node
/**
 * Instrument check for the PERFMODE-15 footprint capture: does the page the
 * capture drives make the renderer keep response bodies?
 *
 * Serves BODY_COUNT no-store bodies of BODY_MB each from a loopback server,
 * fetches them all in one page and drops them, collects garbage, then reads
 * the renderer's Blink buffer partition from a Chromium memory-infra dump.
 * Prints one JSON line: {"page": <kind>, "pa_buffer_mb": <n>, "bodies_mb": <n>}.
 *
 *   node scripts/perf/network_buffer_probe.mjs --page playwright      (control)
 *   node scripts/perf/network_buffer_probe.mjs --page uninstrumented  (what the capture uses)
 *
 * Run from apps/webui/frontend so @playwright/test resolves.
 */
import { createServer } from 'node:http';
import { createRequire } from 'node:module';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { parseArgs } from 'node:util';

import { openUninstrumentedPage } from './uninstrumented-page.mjs';

const BODY_COUNT = 12;
const BODY_MB = 4;

const { values } = parseArgs({ options: { page: { type: 'string' } } });
if (values.page !== 'playwright' && values.page !== 'uninstrumented') {
	console.error('usage: node network_buffer_probe.mjs --page <playwright|uninstrumented>');
	process.exit(2);
}

const resolver = createRequire(path.join(process.cwd(), 'package.json'));
const playwright = await import(pathToFileURL(resolver.resolve('@playwright/test')).href);
const { chromium } = playwright.default ?? playwright;

const body = Buffer.alloc(BODY_MB * 1024 * 1024, 7);
const server = createServer((request, response) => {
	if (request.url === '/') {
		response.writeHead(200, { 'content-type': 'text/html' });
		response.end('<!doctype html><title>probe</title>');
		return;
	}
	response.writeHead(200, { 'content-type': 'application/octet-stream', 'cache-control': 'no-store' });
	response.end(body);
});
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const origin = `http://127.0.0.1:${server.address().port}`;

/** The renderer's dump is the one carrying the largest V8 heap. */
function rendererBufferMb(events) {
	let best = null;
	for (const event of events) {
		const allocators = event.args?.dumps?.allocators;
		if (event.ph !== 'v' || allocators?.v8 === undefined) continue;
		const v8 = parseInt(allocators.v8.attrs.size.value, 16);
		if (best === null || v8 > best.v8) best = { v8, allocators };
	}
	const buffer = best?.allocators['partition_alloc/partitions/buffer'];
	if (buffer === undefined) throw new Error('no renderer dump with a buffer partition: cannot measure');
	return parseInt(buffer.attrs.size.value, 16) / (1024 * 1024);
}

async function memoryDump(browser) {
	const session = await browser.newBrowserCDPSession();
	const events = [];
	session.on('Tracing.dataCollected', (event) => events.push(...event.value));
	const complete = new Promise((resolve) => session.once('Tracing.tracingComplete', resolve));
	await session.send('Tracing.start', {
		traceConfig: {
			includedCategories: ['disabled-by-default-memory-infra'],
			memoryDumpConfig: { triggers: [] }
		}
	});
	const { success } = await session.send('Tracing.requestMemoryDump', {
		deterministic: true,
		levelOfDetail: 'detailed'
	});
	await session.send('Tracing.end');
	await complete;
	await session.detach();
	if (!success) throw new Error('Tracing.requestMemoryDump reported failure: cannot measure');
	return events;
}

const browser = await chromium.launch({ headless: true });
try {
	let page;
	let collectGarbage;
	if (values.page === 'playwright') {
		page = await (await browser.newContext()).newPage();
		const cdp = await page.context().newCDPSession(page);
		collectGarbage = () => cdp.send('HeapProfiler.collectGarbage');
	} else {
		page = await openUninstrumentedPage(browser);
		collectGarbage = () => page.send('HeapProfiler.collectGarbage');
	}
	await page.goto(`${origin}/`);
	const fetched = await page.evaluate(
		async ({ count }) => {
			let bytes = 0;
			for (let i = 0; i < count; i += 1) {
				const response = await fetch(`/body/${i}`);
				bytes += (await response.arrayBuffer()).byteLength;
			}
			return bytes;
		},
		{ count: BODY_COUNT }
	);
	if (fetched !== BODY_COUNT * body.length) throw new Error(`fetched ${fetched} bytes, expected ${BODY_COUNT * body.length}`);
	await collectGarbage();
	await collectGarbage();
	const paBufferMb = rendererBufferMb(await memoryDump(browser));
	console.log(JSON.stringify({ page: values.page, pa_buffer_mb: Number(paBufferMb.toFixed(1)), bodies_mb: BODY_COUNT * BODY_MB }));
} finally {
	await browser.close();
	server.close();
}
