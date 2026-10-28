// requirement: USBEXP-01
// requirement: USBEXP-02
// requirement: USBEXP-03
// requirement: USBEXP-04
/**
 * The USB export screen and a stick's right-click rows, in Library mode,
 * against the root suite's real engine. No answer is stubbed: every answer the
 * screen shows comes from the engine's own routes. One test does intercept: the
 * late-read test holds a real `GET /api/v1/usb/volumes` in the browser and then
 * resets its connection, a transport fault injected to order a late failure.
 * It fabricates no response body; the newer read it is compared against is the
 * engine's own answer.
 *
 * What the engine can provide decides what runs. Each test asks the real
 * `GET /api/v1/usb/volumes` first:
 * - discovery refused (Linux CI, any non-macOS host): the screen must show
 *   the engine's refusal sentence, and a real eject must answer 503;
 * - a present USB stick (a Mac with one plugged in): the stick's tree offers
 *   USB status / Sync / Eject on right-click, and Check plan (writes nothing)
 *   shows either a plan or the daemon's own refusal code.
 * The branch a host cannot reach is reported UNAVAILABLE as a skip that says
 * what is missing, never as a pass.
 *
 * Regression one-liners:
 *   - if the bottom bar's export/eject button opens no screen in Library mode then broken
 *   - if a host without discovery shows "no sticks" instead of the engine's refusal then broken
 *   - if eject answers anything but 503 usb_eject_unavailable where discovery is refused then broken
 *   - if right-clicking a stick in Library mode shows no USB status / Sync / Eject rows then broken
 *   - if Check plan on a real stick shows neither a plan nor the daemon's code then broken
 *   - if an older volume read failing after a newer one answered changes what the screen says then broken
 *   - if an in-app route change or a page close unmounts the screen while a plan is in flight then broken
 */
import { expect, test, type APIRequestContext, type Page, type Route } from '@playwright/test';

import { spendBootLanding } from './support/boot-landing';

type Volume = { id: string; name: string; mount_path: string | null; present: boolean; role: string; protocol?: string | null; simulated?: boolean };
type Discovery = { status: number; reason: string | null; sticks: Volume[] };

async function discovery(request: APIRequestContext): Promise<Discovery> {
	const response = await request.get('/api/v1/usb/volumes');
	const body = await response.json();
	if (response.status() === 503) return { status: 503, reason: body.detail?.reason ?? null, sticks: [] };
	expect(response.status(), JSON.stringify(body)).toBe(200);
	const sticks = (body.volumes as Volume[]).filter(
		(v) => v.present && v.role === 'usb_stick' && v.protocol === 'USB' && v.simulated !== true && v.mount_path !== null
	);
	return { status: 200, reason: null, sticks };
}

async function openCrateDesk(page: Page): Promise<void> {
	await page.goto('/');
	await expect(page.getByTestId('library-workspace')).toBeVisible({ timeout: 30_000 });
	await expect(page.getByTestId('browser-panel')).toBeVisible({ timeout: 30_000 });
}

test.describe('USB export screen on the real engine (Library mode)', () => {
	test.beforeEach(async ({ page }) => {
		await spendBootLanding(page);
	});

	test('where discovery is refused, the screen shows the refusal and eject answers 503', async ({ page, request }) => {
		const found = await discovery(request);
		test.skip(
			found.status !== 503,
			'UNAVAILABLE: this engine has USB discovery (macOS), so the refused state cannot be observed here'
		);
		const eject = await request.post('/api/v1/usb/eject', { data: { mount_path: '/Volumes/NO-SUCH-STICK', volume_id: 'vol:00000000-0000-0000-0000-000000000000' } });
		expect(eject.status()).toBe(503);
		expect((await eject.json()).detail.code).toBe('usb_eject_unavailable');

		await openCrateDesk(page);
		await page.getByTestId('bottom-bar-export-eject').click();
		const screen = page.getByTestId('usb-export-screen');
		await expect(screen).toBeVisible({ timeout: 30_000 });
		// Modal in the top layer: the page underneath is inert, so no menu or
		// shortcut target there can navigate away mid-operation (Codex P1 4286889359).
		expect(await screen.evaluate((el) => el.matches(':modal')), 'if the screen is not a modal dialog, the page under it stays live - broken').toBe(true);
		expect(
			await page.getByTestId('bottom-bar-export-eject').evaluate((el) => el.closest('[inert]') !== null || !el.checkVisibility() || document.elementFromPoint(el.getBoundingClientRect().x + 2, el.getBoundingClientRect().y + 2) !== el),
			'the bottom bar button under the screen must not be hittable'
		).toBe(true);
		await page.keyboard.press('Escape');
		await expect(screen, 'Escape still closes the screen when nothing is running').toBeHidden();
		await page.getByTestId('bottom-bar-export-eject').click();
		await expect(screen).toBeVisible();
		const notice = page.getByTestId('usb-export-discovery');
		await expect(notice).toContainText('USB discovery unavailable');
		// The refusal must carry its sentence: a missing one is the defect, not
		// an empty string every notice contains.
		expect(typeof found.reason === 'string' && found.reason.trim() !== '', `503 discovery reason: ${JSON.stringify(found.reason)}`).toBe(true);
		await expect(notice).toContainText(found.reason as string);
		await expect(page.getByTestId('usb-export-stick')).toHaveCount(0);
		await expect(page.getByTestId('usb-export-plan')).toBeDisabled();
		await expect(page.getByTestId('usb-export-plan')).toHaveAttribute('title', 'Pick a stick first');
	});

	// Reads overlap: the poll, the screen opening, the rescan after an eject.
	// Only the newest may write the list, or an older answer landing late puts
	// back what a newer one already replaced (Codex P2 4288381672, P1 4288481438).
	// Every answer here is the engine's own: the older read is held in the
	// browser, then its connection is aborted after the newer read answered.
	test('an older volume read failing after a newer one answered changes nothing on screen', async ({ page, request }) => {
		const found = await discovery(request);
		await openCrateDesk(page);

		let held: Route | null = null;
		let heldReady!: () => void;
		const heldPromise = new Promise<void>((resolve) => (heldReady = resolve));
		await page.route('**/api/v1/usb/volumes', async (route) => {
			if (held === null) {
				held = route;
				heldReady();
				return;
			}
			await route.continue();
		});

		const screen = page.getByTestId('usb-export-screen');
		await page.getByTestId('bottom-bar-export-eject').click();
		await expect(screen).toBeVisible({ timeout: 30_000 });
		await heldPromise;
		await page.keyboard.press('Escape');
		await expect(screen).toBeHidden();

		const answered = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/v1/usb/volumes');
		await page.getByTestId('bottom-bar-export-eject').click();
		await expect(screen).toBeVisible();
		await answered;
		const notice = page.getByTestId('usb-export-discovery');
		if (found.status === 503) {
			await expect(notice).toContainText('USB discovery unavailable');
		} else {
			await expect(notice).toHaveCount(0);
		}
		const before = (await notice.count()) === 0 ? null : await notice.textContent();

		const failed = page.waitForEvent('requestfailed', (r) => new URL(r.url()).pathname === '/api/v1/usb/volumes');
		await (held as Route | null)?.abort('connectionreset');
		await failed;
		// Give the failed read's handler its turn before reading the screen.
		await page.evaluate(() => new Promise((resolve) => setTimeout(resolve, 250)));
		const after = (await notice.count()) === 0 ? null : await notice.textContent();
		expect(after, 'the late failure of an older read must not replace what the newer read put on screen').toBe(before);
		await page.unroute('**/api/v1/usb/volumes');
	});

	test('right-clicking a stick offers USB status, Sync to stick and Eject stick', async ({ page, request }) => {
		const found = await discovery(request);
		test.skip(
			found.sticks.length === 0,
			found.status === 503
				? `UNAVAILABLE: USB discovery is refused on this engine (${found.reason}), so there is no stick tree to right-click`
				: 'UNAVAILABLE: no USB stick is plugged in, so there is no stick tree to right-click'
		);
		await openCrateDesk(page);
		await page.getByTestId('library-source-usbs').click();
		const row = page.getByTestId('usb-source-row').filter({ hasText: found.sticks[0].name });
		await row.click();
		const tree = page.getByTestId('usb-stick-tree');
		await expect(tree).toBeVisible();
		await tree.click({ button: 'right' });
		const menu = page.getByTestId('context-menu');
		await expect(menu.getByRole('menuitem', { name: 'USB status...' })).toBeVisible();
		await expect(menu.getByRole('menuitem', { name: 'Eject stick' })).toBeVisible();
		const sync = menu.getByRole('menuitem', { name: 'Sync to stick' });
		await expect(sync).toHaveAttribute('aria-disabled', 'true');
		// USB status opens the screen; nothing is ejected here.
		await menu.getByRole('menuitem', { name: 'USB status...' }).click();
		await expect(page.getByTestId('usb-export-screen')).toBeVisible();
		await expect(page.getByTestId('usb-export-stick').filter({ hasText: found.sticks[0].name })).toBeVisible();
	});

	test('Check plan on a real stick shows a plan or the daemon refusal, and writes nothing', async ({ page, request }) => {
		const found = await discovery(request);
		test.skip(
			found.sticks.length === 0,
			found.status === 503
				? `UNAVAILABLE: USB discovery is refused on this engine (${found.reason}), so no stick can be picked`
				: 'UNAVAILABLE: no USB stick is plugged in, so no stick can be picked'
		);
		await openCrateDesk(page);
		await page.getByTestId('bottom-bar-export-eject').click();
		const stick = page.getByTestId('usb-export-stick').filter({ hasText: found.sticks[0].name });
		await stick.getByRole('radio').check();
		// A template path is supplied so the client's own checks pass and the request has to
		// reach the plan route: the stick's own rekordbox export database. Whether it is a usable
		// template is the daemon's call, so either a plan or a daemon refusal can come back.
		await page.getByTestId('usb-export-template').fill(`${found.sticks[0].mount_path}/PIONEER/rekordbox/exportLibrary.db`);
		await page.getByTestId('usb-export-playlist').fill('USB export e2e');
		const planResponse = page.waitForResponse(
			(res) => res.url().includes('/api/v1/usb-export/plan') && res.request().method() === 'POST',
			{ timeout: 30_000 }
		);
		await page.getByTestId('usb-export-plan').click();
		// The request really left the page: client-side validation never sends one.
		const answered = await planResponse;
		const outcome = page.getByTestId('usb-export-plan-summary').or(page.getByTestId('usb-export-problem'));
		await expect(outcome).toBeVisible({ timeout: 30_000 });
		if (answered.ok()) {
			await expect(page.getByTestId('usb-export-plan-summary')).toBeVisible();
		} else {
			// The daemon's own code, never a bare HTTP status and never a client-side check.
			const problem = page.getByTestId('usb-export-problem');
			await expect(problem).toHaveText(/^[a-z_]+: \S/);
			await expect(problem).not.toHaveText(/^(target_missing|template_missing|playlist_name_missing|track_ids_invalid):/);
		}
		await expect(page.getByTestId('usb-export-done')).toHaveCount(0);
	});
	test('a route change or page close while a plan is in flight is refused, and the screen says why', async ({ page, request }) => {
		const found = await discovery(request);
		test.skip(
			found.sticks.length === 0,
			found.status === 503
				? `UNAVAILABLE: USB discovery is refused on this engine (${found.reason}), so no plan can be started`
				: 'UNAVAILABLE: no USB stick is plugged in, so no plan can be started'
		);
		await openCrateDesk(page);
		await page.getByTestId('bottom-bar-export-eject').click();
		const stick = page.getByTestId('usb-export-stick').filter({ hasText: found.sticks[0].name });
		await stick.getByRole('radio').check();
		await page.getByTestId('usb-export-template').fill(`${found.sticks[0].mount_path}/PIONEER/rekordbox/exportLibrary.db`);
		await page.getByTestId('usb-export-playlist').fill('USB export e2e');
		// Hold the real plan request so the operation stays in flight; it is
		// continued to the real engine once the refusal has been observed.
		const holder: { route: Route | null } = { route: null };
		const held = new Promise<void>((resolve) => {
			void page.route('**/api/v1/usb-export/plan', (route: Route) => {
				holder.route = route;
				resolve();
			});
		});
		await page.getByTestId('usb-export-plan').click();
		await held;
		const before = new URL(page.url()).pathname;
		// A real in-app link, routed by the SvelteKit client like any other.
		await page.evaluate(() => {
			const link = document.createElement('a');
			link.href = '/performance';
			link.textContent = 'leave';
			document.body.appendChild(link);
			link.click();
			link.remove();
		});
		await expect(page.getByTestId('usb-export-busy-refused')).toBeVisible();
		await expect(page.getByTestId('usb-export-screen')).toBeVisible();
		expect(new URL(page.url()).pathname).toBe(before);
		// Closing the page is refused too: the browser asks before leaving.
		const leavePrompt = page.waitForEvent('dialog');
		await page.close({ runBeforeUnload: true });
		const dialog = await leavePrompt;
		expect(dialog.type()).toBe('beforeunload');
		await dialog.dismiss();
		await expect(page.getByTestId('usb-export-screen')).toBeVisible();
		await holder.route?.continue();
		await page.unroute('**/api/v1/usb-export/plan');
		const outcome = page.getByTestId('usb-export-plan-summary').or(page.getByTestId('usb-export-problem'));
		await expect(outcome).toBeVisible({ timeout: 30_000 });
	});
});
