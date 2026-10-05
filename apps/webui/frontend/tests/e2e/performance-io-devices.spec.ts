// requirement: IOPIN-14 (the audio I/O panel never draws an unreadable device list as an empty one)
// [if] the page boots and nobody has opened I/O [then] the device list has already been read, and the engine's parity route says how ⛔️
// [if] I/O is opened on a machine with an output [then] the system default output is listed with a non-empty label and no row is blank ⛔️
// [if] the browser withholds device names [then] the panel shows a named state with a grant or retry action, not an empty menu ⛔️
// [if] device access is granted [then] every output the browser itself names is in the panel, and the parity route returns the same list ⛔️
// [if] the device API is missing, rejects, or hangs [then] each renders its own named state with a retry action ⛔️
//
// Regression line: if the panel lists zero selectable outputs, or shows a blank row, on a
// machine whose browser reports an audio output, the I/O panel is broken (observed live Thu 1
// Oct 2026: three selects holding only their placeholders, no notice, `supported: false`).
//
// The first four tests drive the REAL `navigator.mediaDevices` of the browser under test: no
// device is faked. The last block is fault injection, labelled as such, because a missing or
// hanging device API cannot be produced any other way on a healthy machine.
import { expect, test, type Page } from '@playwright/test';

type Listed = { id: string; label: string };
type Access = { status: string; action: string; message: string | null; detail: string | null; notices: string[] };

async function bootPerformance(page: Page): Promise<void> {
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
}

async function headphones(page: Page): Promise<{ outputs: Listed[]; inputs: Listed[]; device_access: Access }> {
	return page.evaluate(() => {
		const hp = window.musicDjToolsPerformance!.query().mixer.headphones as unknown as {
			outputs: Listed[];
			inputs: Listed[];
			device_access: Access;
		};
		return JSON.parse(JSON.stringify({ outputs: hp.outputs, inputs: hp.inputs, device_access: hp.device_access }));
	});
}

/** What the browser itself says, asked directly: the control for every claim below. */
async function rawDevices(page: Page): Promise<{ kind: string; deviceId: string; label: string }[]> {
	return page.evaluate(async () =>
		(await navigator.mediaDevices.enumerateDevices()).map((device) => ({
			kind: device.kind,
			deviceId: device.deviceId,
			label: device.label
		}))
	);
}

async function openIo(page: Page) {
	await page.getByRole('button', { name: 'SHOW AUDIO I/O' }).click();
	const panel = page.getByRole('dialog', { name: 'Audio I/O settings' });
	await expect(panel).toBeVisible();
	return panel;
}

async function selectableOptions(page: Page, label: string): Promise<{ value: string; text: string }[]> {
	return page
		.getByLabel(label, { exact: true })
		.locator('option:not([disabled])')
		.evaluateAll((options) =>
			options.map((option) => ({
				value: (option as HTMLOptionElement).value,
				text: (option.textContent ?? '').trim()
			}))
		);
}

test('the device list is read at boot, before anyone opens I/O', async ({ page }) => {
	await bootPerformance(page);
	await expect
		.poll(async () => (await headphones(page)).device_access?.status, {
			message: 'boot must enumerate devices; "not_checked" means the enumeration never ran'
		})
		.not.toBe('not_checked');
	const state = await headphones(page);
	expect(state.device_access.status).toMatch(/^(listed|permission_needed|permission_denied)$/);
	expect(state.outputs.length).toBeGreaterThan(0);
});

test('I/O lists the system default output with a non-empty label and names a withheld list', async ({ page }) => {
	await bootPerformance(page);
	const raw = await rawDevices(page);
	const panel = await openIo(page);

	for (const select of ['master output device', 'headphone output device']) {
		await expect
			.poll(async () => (await selectableOptions(page, select)).length, {
				message: `${select} must offer at least the system default output`
			})
			.toBeGreaterThan(0);
		const options = await selectableOptions(page, select);
		// Presence of the good thing: a default the operator can read and pick.
		const systemDefault = options.find((option) => option.value === 'default');
		expect(systemDefault, `${select} has no system default row: ${JSON.stringify(options)}`).toBeDefined();
		expect(systemDefault!.text).not.toBe('');
		// And no blank row anywhere: an empty id or label is the browser's
		// placeholder, which is the exact thing that used to be drawn.
		for (const option of options) {
			expect(option.value, `blank value in ${select}`).not.toBe('');
			expect(option.text, `blank label in ${select}`).not.toBe('');
		}
	}

	const access = panel.locator('[data-io-device-access]');
	await expect(access).toHaveAttribute('data-io-device-access', /^(listed|permission_needed|permission_denied)$/);
	const status = await access.getAttribute('data-io-device-access');
	const withheldByBrowser = raw.some(
		(device) => device.kind.startsWith('audio') && (device.deviceId === '' || device.label === '')
	);
	if (withheldByBrowser) {
		// The browser held names back, so `listed` here would be a lie.
		expect(status).not.toBe('listed');
		await expect(panel.locator('[data-io-device-access-message]')).not.toBeEmpty();
		await expect(panel.locator('[data-io-device-access-action]')).toBeVisible();
	}
});

test('with device access granted, every output the browser names is listed', async ({ page, context }) => {
	await context.grantPermissions(['microphone']);
	await bootPerformance(page);
	const raw = await rawDevices(page);
	const named = raw.filter((device) => device.kind === 'audiooutput' && device.deviceId !== '' && device.label !== '');
	// UNKNOWN, not a pass: a host whose browser names no output cannot measure this.
	test.skip(named.length === 0, 'this host names no audio output even with access granted; nothing to compare');

	await openIo(page);
	await expect.poll(async () => (await headphones(page)).device_access.status).toBe('listed');
	const state = await headphones(page);
	for (const device of named) {
		expect(state.outputs, `output ${device.label} missing from the panel state`).toContainEqual({
			id: device.deviceId,
			label: device.label
		});
	}
	const options = await selectableOptions(page, 'master output device');
	expect(options.map((option) => option.text)).toEqual(state.outputs.map((output) => output.label));
	await expect(page.locator('[data-io-device-access-action]')).toHaveCount(0);
});

test('the engine parity route returns the same device list the panel shows', async ({ page }) => {
	await bootPerformance(page);
	await openIo(page);
	await expect.poll(async () => (await headphones(page)).device_access.status).not.toBe('not_checked');
	const shown = await headphones(page);
	// GET is the read-only mirror an agent polls. It answers 503 until the page
	// has published a mirror, and the mirror is published once a second, so the
	// route trails the panel by up to that long: poll for the state, not for a 200.
	await expect
		.poll(
			async () => {
				const response = await page.request.get('/api/v1/performance/headphones');
				if (response.status() !== 200) return `HTTP ${response.status()}`;
				return (await response.json()).device_access?.status;
			},
			{ timeout: 30_000 }
		)
		.toBe(shown.device_access.status);
	const mirrored = await (await page.request.get('/api/v1/performance/headphones')).json();
	expect(mirrored.outputs).toEqual(shown.outputs);
	expect(mirrored.inputs).toEqual(shown.inputs);
	expect(mirrored.outputs.some((output: Listed) => output.id === 'default' && output.label !== '')).toBe(true);
	// POST runs the same enumeration through the page and returns the state it produced.
	const refreshed = await page.request.post('/api/v1/performance/headphones/outputs/refresh');
	expect(refreshed.status(), await refreshed.text()).toBe(200);
	const fromRoute = await refreshed.json();
	expect(fromRoute.outputs).toEqual(shown.outputs);
	expect(fromRoute.inputs).toEqual(shown.inputs);
	expect(fromRoute.device_access.status).toBe(shown.device_access.status);
});

test.describe('fault injection: a device API that cannot be asked', () => {
	const faults: { name: string; status: string; install: () => void }[] = [
		{
			name: 'missing',
			status: 'api_missing',
			install: () => {
				Object.defineProperty(navigator, 'mediaDevices', { value: undefined, configurable: true });
			}
		},
		{
			name: 'rejecting',
			status: 'enumeration_failed',
			install: () => {
				navigator.mediaDevices.enumerateDevices = () => Promise.reject(new DOMException('injected', 'NotReadableError'));
			}
		},
		{
			name: 'hanging',
			status: 'timeout',
			install: () => {
				navigator.mediaDevices.enumerateDevices = () => new Promise(() => {});
			}
		}
	];
	for (const fault of faults) {
		test(`a ${fault.name} device API renders ${fault.status} with a retry, never an empty list`, async ({ page }) => {
			await page.addInitScript(fault.install);
			await bootPerformance(page);
			const panel = await openIo(page);
			const access = panel.locator('[data-io-device-access]');
			await expect(access).toHaveAttribute('data-io-device-access', fault.status, { timeout: 20_000 });
			await expect(panel.locator('[data-io-device-access-message]')).not.toBeEmpty();
			await expect(panel.locator('[data-io-device-access-action]')).toHaveAttribute('data-io-device-access-action', 'retry');
			const state = await headphones(page);
			expect(state.outputs).toContainEqual({ id: 'default', label: 'System default output' });
		});
	}
});
