/**
 * CUEOUT-21 delivery check: does the interference warning reach the operator's
 * screen, in the real modal, on the real machine?
 *
 * Asserts the rendered DOM, not the endpoint: the endpoint answering 200 was
 * already proven by curl, and a line that never renders is the failure mode
 * this repo keeps hitting.
 */
import { chromium } from '@playwright/test';
const arg = (n, d = null) => { const i = process.argv.indexOf(`--${n}`); return i === -1 ? d : process.argv[i + 1]; };
const URL_ = String(arg('url', 'http://localhost:9464/performance'));
const CUE_MATCH = String(arg('cue', 'MacBook Pro Speakers'));

const results = [];
const check = (name, ok, detail = '') => {
	results.push(ok);
	console.log(`${ok ? '[OK]  ' : '[FAIL]'} ${name}${detail === '' ? '' : `  ${detail}`}`);
};

const browser = await chromium.launch({ headless: false, timeout: 600_000,
	args: ['--autoplay-policy=no-user-gesture-required', '--use-fake-ui-for-media-stream'] });
try {
	const page = await browser.newPage();
	const consoleErrors = [];
	page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()); });
	await page.goto(URL_, { waitUntil: 'domcontentloaded', timeout: 180_000 });
	await page.waitForFunction(() => window.musicDjToolsPerformance !== undefined, { timeout: 120_000 });
	await page.mouse.click(5, 5);

	const wire = await page.evaluate(async () => (await fetch('/api/v1/audio-interference')).json());
	check('the engine answers the interference route', wire.supported === true,
		`detected=${wire.detected.map((d) => d.label).join(', ') || '(none)'}`);

	// Pick any cue output so CALIBRATE is reachable; no run is started.
	const picked = await page.evaluate(async (needle) => {
		const ipc = window.musicDjToolsPerformance;
		await ipc.dispatch({ type: 'headphone_outputs_refresh' });
		const hp = ipc.query().mixer.headphones;
		const hits = hp.outputs.filter((d) => d.label.toLowerCase().includes(needle.toLowerCase()));
		const dev = hits.find((d) => d.id !== 'default' && !/^default - /i.test(d.label)) ?? hits[0];
		if (dev === undefined) return null;
		await ipc.dispatch({ type: 'headphone_output_select', device_id: dev.id });
		return dev.label;
	}, CUE_MATCH);
	check('a cue output could be selected', picked !== null, String(picked));

	const overlay = page.locator('[aria-label="First-run setup"] button.su-min');
	if (await overlay.count() > 0) await overlay.first().click().catch(() => {});
	await page.getByRole('button', { name: /CALIBRATE/i }).first().click();
	const panel = page.locator('.ca-panel, [role="dialog"]').first();
	await panel.waitForTimeout?.(0);
	await page.waitForTimeout(1500);

	const body = await panel.innerText();
	const expected = wire.detected.length > 0;
	const shown = /Quit it before calibrating/.test(body);
	check('the warning renders BEFORE a run is started, matching the engine',
		shown === expected, expected ? `expected a warning, shown=${shown}` : `expected none, shown=${shown}`);
	if (expected) {
		for (const d of wire.detected) {
			check(`the warning names ${d.label}`, body.includes(d.label));
		}
		check('it does not claim to know who holds the device',
			!/is holding|has taken/i.test(body));
	}
	check('no console errors from the page', consoleErrors.length === 0, consoleErrors.slice(0, 2).join(' | '));

	await page.screenshot({ path: 'cue-align-interference.png' });
	console.log('screenshot: apps/webui/frontend/cue-align-interference.png');
	console.log(`\n${results.filter(Boolean).length}/${results.length} checks passed`);
	if (results.some((r) => !r)) process.exitCode = 1;
} finally { await browser.close(); }
