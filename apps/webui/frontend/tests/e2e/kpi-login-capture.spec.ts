import { test, type Page, type Request } from '@playwright/test';
import { writeFileSync } from 'node:fs';
import { kpiCaptureTimeoutS } from './kpi-capture-timeouts.mjs';

const RESULT_PATH = process.env.KPI_CAPTURE_RESULT;
const FRONTEND_ORIGIN = (
	process.env.KPI_CAPTURE_FRONTEND ??
	process.env.PERFORMANCE_E2E_BASE_URL ??
	'http://127.0.0.1:5273'
).replace(/\/$/, '');
const TIMEOUT_S = kpiCaptureTimeoutS(process.env.KPI_CAPTURE_TIMEOUT_S);
const LOGIN_SUBMIT_KEY = 'mdt.loginSubmitAt';
const AUTH_ME_PATH = '/api/v1/auth/me';
const CLIENT_EVENTS_PATH = '/api/v1/client-events';
const AUTH_LOGIN_PATH = '/api/v1/auth/login';
const GOOGLE_HOST_FRAGMENT = 'accounts.google.com';
/** A click that really submitted produces this POST within a second or two. */
const LOGIN_POST_WAIT_MS = 20_000;
/** One retry is enough for the bauble race; more would mask a real fault. */
const SIGN_IN_CLICK_ATTEMPTS = 2;
/** Consent-screen affordances, by exact visible label. */
const GOOGLE_PROCEED = /^(continue|allow|next|confirm)$/i;
/** Chooser controls that are NOT an account row. `remove` is load-bearing:
 * the chooser carries a hidden "Yes, remove" dialog we must never click. */
const GOOGLE_CHOOSER_NOISE = /use another account|remove|cancel|done|sign out|forgot/i;
/** Google's own screens are not the KPI; cap the time spent on them. */
const GOOGLE_DRIVE_MAX_MS = 45_000;
const GOOGLE_DRIVE_MAX_ROUNDS = 8;

interface CaptureResult {
	ok: boolean;
	span: Record<string, unknown> | null;
	reason: string | null;
}

function writeResult(result: CaptureResult): void {
	if (!RESULT_PATH) {
		throw new Error('KPI_CAPTURE_RESULT is required');
	}
	writeFileSync(RESULT_PATH, JSON.stringify(result), 'utf-8');
}

function isLoginPerfSpan(body: Record<string, unknown> | null | undefined): boolean {
	return body?.kind === 'perf-span' && body?.name === 'login-submit-to-library-usable';
}

function sleep(ms: number): Promise<void> {
	return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * The capture budget is ONE deadline across every phase, not a fresh budget
 * per wait. The config's test timeout is this plus fixed headroom, so the spec
 * always reaches its own withhold path before Playwright tears the page down.
 */
function makeBudget(totalMs: number): () => number {
	const deadline = Date.now() + totalMs;
	return () => Math.max(1_000, deadline - Date.now());
}

interface SpanCollector {
	take(): Record<string, unknown> | null;
	reset(): void;
}

/**
 * Arm span collection BEFORE the click that can produce one.
 *
 * The span is posted the moment the library reaches first paint, which is
 * inside the post-login boot -- earlier than any code that runs after
 * `waitForURL` resolves. A one-shot `page.waitForRequest` registered after the
 * fact therefore misses a span that DID fire and reports missing-telemetry.
 * A page-level listener armed before the click cannot miss it: page events
 * survive navigation, including the round trip through Google.
 */
function armLoginSpanCollector(page: Page): SpanCollector {
	let captured: Record<string, unknown> | null = null;
	page.on('request', (request: Request) => {
		if (request.method() !== 'POST') return;
		if (!request.url().includes(CLIENT_EVENTS_PATH)) return;
		let body: Record<string, unknown>;
		try {
			body = request.postDataJSON() as Record<string, unknown>;
		} catch {
			return;
		}
		if (!isLoginPerfSpan(body)) return;
		captured = body;
	});
	return {
		take: () => captured,
		reset: () => {
			captured = null;
		}
	};
}

async function waitForCollectedSpan(
	page: Page,
	collector: SpanCollector,
	timeoutMs: number
): Promise<Record<string, unknown> | null> {
	const deadline = Date.now() + timeoutMs;
	for (;;) {
		const span = collector.take();
		if (span !== null) return span;
		if (page.isClosed()) return null;
		if (Date.now() >= deadline) return null;
		await sleep(100);
	}
}

/**
 * The bauble renders its SIGNED-OUT face while the deferred `refreshUser` is
 * still outstanding (UserBauble.svelte defers it out of the boot burst), so
 * "Sign in with Google" being visible does not mean `auth.user` is null yet.
 * Clicking inside that window opens the account menu instead of calling
 * `startLogin`, no marks are written, and the capture withholds with
 * "restored-session" while blaming the storageState. Waiting for the daemon's
 * own answer is the settle point; the bauble's face is final afterwards.
 */
async function waitForAuthSettled(page: Page, timeoutMs: number): Promise<void> {
	await page
		.waitForResponse((response) => response.url().includes(AUTH_ME_PATH), {
			timeout: timeoutMs
		})
		.catch(() => null);
}

/**
 * Click through Google's account chooser and consent screen.
 *
 * `build_authorization_url` sends `prompt=consent` (apps/webui/server/auth.py),
 * which makes Google park on the chooser and then the consent screen on EVERY
 * sign-in, live session or not. So replaying a pre-consented storageState --
 * the only way this capture can run unattended -- stalls on a screen that
 * needs a click, and the capture withheld with "stuck on Google" while the
 * session was in fact perfectly valid.
 *
 * Driving these clicks does not touch the scored number: the KPI is
 * `pre_navigate_ms + post_navigate_ms`, which excludes Google by construction.
 * Every failure here falls through to the caller's existing "stuck on Google"
 * withhold rather than throwing.
 */
async function clickOneGoogleAffordance(page: Page): Promise<boolean> {
	const proceed = page.getByRole('button', { name: GOOGLE_PROCEED });
	const proceedCount = await proceed.count().catch(() => 0);
	for (let index = 0; index < proceedCount; index += 1) {
		const button = proceed.nth(index);
		if (!(await button.isVisible().catch(() => false))) continue;
		await button.click({ timeout: 5_000 }).catch(() => null);
		return true;
	}
	// An account row is the one control carrying an email address; matching on
	// that rather than on a name keeps a real identity out of this repository.
	const rows = page.locator('[role="link"], [role="button"], li');
	const rowCount = Math.min(await rows.count().catch(() => 0), 40);
	for (let index = 0; index < rowCount; index += 1) {
		const row = rows.nth(index);
		if (!(await row.isVisible().catch(() => false))) continue;
		const text = (await row.innerText().catch(() => '')).trim();
		if (text === '' || !text.includes('@')) continue;
		if (GOOGLE_CHOOSER_NOISE.test(text)) continue;
		await row.click({ timeout: 5_000 }).catch(() => null);
		return true;
	}
	return false;
}

async function driveGoogleInterstitials(page: Page, budgetMs: () => number): Promise<void> {
	const deadline = Date.now() + Math.min(budgetMs(), GOOGLE_DRIVE_MAX_MS);
	for (let round = 0; round < GOOGLE_DRIVE_MAX_ROUNDS; round += 1) {
		if (page.isClosed()) return;
		if (Date.now() >= deadline) return;
		if (!page.url().includes(GOOGLE_HOST_FRAGMENT)) return;
		await page.waitForLoadState('domcontentloaded', { timeout: 10_000 }).catch(() => null);
		if (!(await clickOneGoogleAffordance(page))) {
			await sleep(1_000);
			continue;
		}
		await sleep(1_500);
	}
}

async function waitForSignInReady(page: Page, budgetMs: () => number) {
	const signInButton = page.getByRole('button', { name: 'Sign in with Google' });
	const signedInButton = page.getByRole('button', { name: /^Signed in as / });
	await Promise.race([
		signInButton.waitFor({ state: 'visible', timeout: budgetMs() }),
		signedInButton.waitFor({ state: 'visible', timeout: budgetMs() })
	]);
	if (await signedInButton.isVisible()) {
		await signedInButton.click();
		await page.getByRole('menuitem', { name: 'Sign out' }).click();
		await signInButton.waitFor({ state: 'visible', timeout: budgetMs() });
	}
	await page.waitForFunction(
		() => {
			const button = document.querySelector(
				'button[aria-label="Sign in with Google"]'
			) as HTMLButtonElement | null;
			return button !== null && !button.disabled;
		},
		{ timeout: budgetMs() }
	);
	return signInButton;
}

/**
 * Click Sign in and PROVE the click started a login, retrying the bauble race.
 *
 * `waitForURL` back to the SPA cannot carry this proof: when the click never
 * navigated, the current URL already satisfies the predicate and the wait
 * passes trivially, so a click that opened the account menu (or a daemon that
 * refused the login) was indistinguishable from a completed round trip and
 * withheld as "restored-session" -- the wrong reason, measured once in 14 runs.
 * The login POST is the presence-of-the-good-thing check: it exists only when
 * `startLogin` actually ran. Returns null on success, else a withhold reason.
 */
async function driveSignInClick(page: Page, budgetMs: () => number): Promise<string | null> {
	for (let attempt = 1; attempt <= SIGN_IN_CLICK_ATTEMPTS; attempt += 1) {
		const signInButton = await waitForSignInReady(page, budgetMs);
		await clearSubmitMark(page);
		const loginPost = page
			.waitForResponse(
				(response) =>
					response.request().method() === 'POST' &&
					response.url().includes(AUTH_LOGIN_PATH),
				{ timeout: Math.min(budgetMs(), LOGIN_POST_WAIT_MS) }
			)
			.catch(() => null);
		await signInButton.click();
		const response = await loginPost;
		if (response !== null && response.ok()) return null;
		if (response !== null) {
			return `cannot start sign-in: daemon refused the login start (HTTP ${response.status()})`;
		}
		// No POST: the click hit the bauble in its signed-in state and opened
		// the account menu instead of calling startLogin. Close it and retry.
		await page.keyboard.press('Escape').catch(() => null);
	}
	return 'sign-in click did not start a login (the bauble stayed in its signed-in state)';
}

async function ensurePerformance(page: Page): Promise<void> {
	if (new URL(page.url()).pathname !== '/performance') {
		await page.goto('/performance');
	}
}

type SubmitMarks =
	| { readable: true; hasSubmit: boolean; hasNavigate: boolean }
	| { readable: false; hasSubmit: false; hasNavigate: false };

/**
 * Read the client's login marks, tolerating the post-login navigation.
 *
 * `page.evaluate` throws "Execution context was destroyed" when the SPA
 * navigates mid-call, which used to crash the capture instead of withholding.
 * This runs on the DIAGNOSTIC path only -- a captured span already proves the
 * marks existed, because `completeLibraryUsable` returns early without them.
 */
async function readSubmitMarks(page: Page, attempts = 5): Promise<SubmitMarks> {
	for (let attempt = 0; attempt < attempts; attempt += 1) {
		if (page.isClosed()) break;
		try {
			const marks = await page.evaluate((key) => {
				const raw = sessionStorage.getItem(key);
				if (raw === null) {
					return { hasSubmit: false, hasNavigate: false };
				}
				try {
					const parsed = JSON.parse(raw) as { t0?: number; tNavigate?: number };
					return {
						hasSubmit: typeof parsed.t0 === 'number',
						hasNavigate: typeof parsed.tNavigate === 'number'
					};
				} catch {
					return { hasSubmit: false, hasNavigate: false };
				}
			}, LOGIN_SUBMIT_KEY);
			return { readable: true, ...marks };
		} catch {
			await sleep(250);
		}
	}
	return { readable: false, hasSubmit: false, hasNavigate: false };
}

/** Drop any mark left over from an earlier lifecycle, so the span we accept
 * can only belong to the click this run drives. */
async function clearSubmitMark(page: Page): Promise<void> {
	await page
		.evaluate((key) => {
			sessionStorage.removeItem(key);
		}, LOGIN_SUBMIT_KEY)
		.catch(() => null);
}

test('capture S13 login submit-to-library-usable span', async ({ page }) => {
	if (!RESULT_PATH) {
		throw new Error('KPI_CAPTURE_RESULT is required');
	}
	const budgetMs = makeBudget(TIMEOUT_S * 1000);
	const collector = armLoginSpanCollector(page);

	const authSettled = waitForAuthSettled(page, budgetMs());
	await page.goto('/performance');
	await authSettled;

	collector.reset();
	const clickReason = await driveSignInClick(page, budgetMs);
	if (clickReason !== null) {
		writeResult({ ok: false, span: null, reason: clickReason });
		return;
	}

	// Only now is a return to the SPA meaningful: we know the page left it.
	await page
		.waitForURL((url) => url.hostname.includes(GOOGLE_HOST_FRAGMENT), {
			timeout: Math.min(budgetMs(), LOGIN_POST_WAIT_MS)
		})
		.catch(() => null);
	await driveGoogleInterstitials(page, budgetMs);

	try {
		await page.waitForURL(
			(url) => {
				if (url.origin !== FRONTEND_ORIGIN) return false;
				return url.pathname === '/' || url.pathname === '/performance';
			},
			{ timeout: budgetMs() }
		);
	} catch {
		const host = new URL(page.url()).hostname;
		if (host.includes('google.')) {
			const title = await page.title().catch(() => 'title unreadable');
			writeResult({
				ok: false,
				span: null,
				reason: `cannot complete real Google login: stuck on Google (${title})`
			});
			return;
		}
		writeResult({
			ok: false,
			span: null,
			reason: 'cannot complete real Google login: callback did not return to the SPA'
		});
		return;
	}

	await ensurePerformance(page);

	const span = await waitForCollectedSpan(page, collector, budgetMs());
	if (span !== null) {
		writeResult({ ok: true, span, reason: null });
		return;
	}

	// No span. Only now do the marks tell us anything: read them to say WHY.
	const marks = await readSubmitMarks(page);
	if (!marks.readable) {
		writeResult({
			ok: false,
			span: null,
			reason:
				'missing-telemetry: no perf-span POST and the page context could not be read to classify it'
		});
		return;
	}
	if (!marks.hasSubmit || !marks.hasNavigate) {
		writeResult({
			ok: false,
			span: null,
			reason:
				'restored-session: submit or navigate mark missing after login (session was restored without driving Sign in)'
		});
		return;
	}
	writeResult({
		ok: false,
		span: null,
		reason:
			'missing-telemetry: login marks present but no perf-span POST (library-usable hooks absent or library did not reach first paint)'
	});
});
