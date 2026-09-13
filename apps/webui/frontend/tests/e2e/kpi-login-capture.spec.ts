import { test, type Locator, type Page, type Request } from '@playwright/test';
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

let resultWritten = false;

function writeResult(result: CaptureResult): void {
	if (!RESULT_PATH) {
		throw new Error('KPI_CAPTURE_RESULT is required');
	}
	writeFileSync(RESULT_PATH, JSON.stringify(result), 'utf-8');
	resultWritten = true;
}

function isLoginPerfSpan(body: Record<string, unknown> | null | undefined): boolean {
	return body?.kind === 'perf-span' && body?.name === 'login-submit-to-library-usable';
}

/**
 * The page's own `Date.now()` at the moment it built the span, or null.
 *
 * `recordPerfSpan` writes `client_timestamp` as an ISO string from that
 * clock. Anything else -- absent, not a string, unparseable -- yields null,
 * and a null is treated as "cannot prove this span is ours".
 */
function spanClientTimestampMs(body: Record<string, unknown>): number | null {
	const raw = body.client_timestamp;
	if (typeof raw !== 'string') return null;
	const parsed = Date.parse(raw);
	return Number.isFinite(parsed) ? parsed : null;
}

function sleep(ms: number): Promise<void> {
	return new Promise((resolve) => setTimeout(resolve, ms));
}

// ----- budget ------------------------------------------------------------

interface Budget {
	/** Milliseconds left, floored at 0. Zero means the deadline has passed. */
	remainingMs(): number;
	expired(): boolean;
	/**
	 * A timeout to hand Playwright. Never 0, because Playwright reads 0 as
	 * "no timeout" -- which is why callers must check `expired()` FIRST rather
	 * than relying on a small number to fail fast for them.
	 */
	waitMs(): number;
}

/**
 * ONE deadline across every phase of the capture.
 *
 * The config's Playwright test timeout is this budget plus fixed headroom
 * (see kpi-capture-timeouts.mjs), so the spec always reaches its own withhold
 * path before the context is torn down. That only holds if no phase can run
 * PAST the deadline: an earlier version floored every remaining-time at
 * 1000ms, so each of a dozen waits could overrun by a second and a short
 * budget drifted well beyond what the operator asked for.
 */
function makeBudget(totalMs: number): Budget {
	const deadline = Date.now() + totalMs;
	const remainingMs = (): number => Math.max(0, deadline - Date.now());
	return {
		remainingMs,
		expired: () => remainingMs() === 0,
		waitMs: () => Math.max(1, remainingMs())
	};
}

/** Withhold naming the phase that ran out, so a short --timeout-s is legible. */
function withholdExpired(phase: string): void {
	writeResult({
		ok: false,
		span: null,
		reason: `capture budget of ${TIMEOUT_S}s expired before ${phase}`
	});
}

// ----- span collection ---------------------------------------------------

interface SpanCollector {
	take(): Record<string, unknown> | null;
	/**
	 * Accept only spans the page itself stamped at or after `gatePageMs`.
	 *
	 * Arming the listener early is what makes the span impossible to MISS;
	 * this gate is what makes it impossible to attribute the WRONG one. A
	 * restored session's own library load posts a span from the PREVIOUS
	 * login, and the window between arming and the click is seconds long
	 * (waitForSignInReady, the sign-out dance, clearing the mark). A stale
	 * span scored as this run's KPI would be a wrong NUMBER, which is worse
	 * than the UNKNOWN this capture exists to remove.
	 *
	 * The gate is the span's OWN `client_timestamp`, not the order in which
	 * Playwright delivered it. Request events cross CDP asynchronously, so a
	 * POST the page issued microseconds BEFORE the gate opened can still be
	 * handed to this listener after it: a flag flipped in the driver is a
	 * fact about the driver, never about the span. `recordPerfSpan` stamps
	 * `client_timestamp` from the page's `Date.now()` at the moment it
	 * builds the payload, and `gatePageMs` is read from that same clock
	 * immediately before the click, so the comparison is total and
	 * monotonic. A span with no usable stamp is REJECTED rather than
	 * guessed at; withholding is the safe direction.
	 */
	openAtClick(gatePageMs: number): void;
	/**
	 * How many login spans were seen but refused as older than the gate.
	 *
	 * Nonzero means the telemetry lifecycle WORKED and the only spans on the
	 * wire belonged to an earlier login, which is a different fault from
	 * telemetry never firing. Reporting one as the other sends the next
	 * reader to the wrong file.
	 */
	staleRejected(): number;
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
	let gatePageMs: number | null = null;
	let staleRejected = 0;
	page.on('request', (request: Request) => {
		if (gatePageMs === null) return;
		if (request.method() !== 'POST') return;
		if (!request.url().includes(CLIENT_EVENTS_PATH)) return;
		let body: Record<string, unknown>;
		try {
			body = request.postDataJSON() as Record<string, unknown>;
		} catch {
			return;
		}
		if (!isLoginPerfSpan(body)) return;
		const stampedAt = spanClientTimestampMs(body);
		if (stampedAt === null || stampedAt < gatePageMs) {
			staleRejected += 1;
			return;
		}
		captured = body;
	});
	return {
		take: () => captured,
		staleRejected: () => staleRejected,
		openAtClick: (openedAtPageMs: number) => {
			captured = null;
			staleRejected = 0;
			gatePageMs = openedAtPageMs;
		}
	};
}

async function waitForCollectedSpan(
	page: Page,
	collector: SpanCollector,
	budget: Budget
): Promise<Record<string, unknown> | null> {
	for (;;) {
		const span = collector.take();
		if (span !== null) return span;
		if (page.isClosed()) return null;
		if (budget.expired()) return null;
		await sleep(Math.min(100, budget.waitMs()));
	}
}

// ----- navigation evidence -----------------------------------------------

interface NavigationLog {
	/** A main-frame navigation to an origin OTHER than the SPA's. */
	reachedOffOrigin(): boolean;
	lastOffOrigin(): string | null;
}

/**
 * Record every main-frame navigation from arming onward.
 *
 * Same discipline as the login POST below, one hop later: waiting for the SPA
 * URL to come BACK cannot prove the page ever left, because the predicate is
 * already satisfied by the URL the page is sitting on. A discarded
 * `waitForURL(google)` left that hole open -- a login POST whose client-side
 * redirect never fired fell straight through to the span wait and was
 * misreported as restored-session or missing-telemetry. An OFF-ORIGIN hop is
 * the positive evidence; "some navigation happened" is not, because a reload
 * or an in-app navigation would satisfy it while the page never left.
 */
function armNavigationLog(page: Page): NavigationLog {
	const seen: string[] = [];
	page.on('framenavigated', (frame) => {
		if (frame !== page.mainFrame()) return;
		seen.push(frame.url());
	});
	const offOrigin = (): string[] =>
		seen.filter((raw) => {
			try {
				return new URL(raw).origin !== FRONTEND_ORIGIN;
			} catch {
				return false;
			}
		});
	return {
		reachedOffOrigin: () => offOrigin().length > 0,
		lastOffOrigin: () => offOrigin().at(-1) ?? null
	};
}

/** Wait for the post-login redirect to actually leave the SPA. */
async function waitForRedirectAway(
	navLog: NavigationLog,
	page: Page,
	budget: Budget,
	windowMs: number
): Promise<boolean> {
	const deadline = Date.now() + windowMs;
	for (;;) {
		// Only an off-origin hop counts. Every sign-in goes through Google's
		// authorize endpoint even when consent is already granted and Google
		// bounces straight back, so nothing legitimate is lost by refusing to
		// accept a navigation that stayed on the SPA.
		if (navLog.reachedOffOrigin()) return true;
		if (page.isClosed()) return false;
		if (budget.expired() || Date.now() >= deadline) return false;
		await sleep(100);
	}
}

// ----- sign-in ------------------------------------------------------------

/**
 * Wait for the daemon's own answer about who is signed in.
 *
 * The bauble renders its SIGNED-OUT face while the deferred `refreshUser` is
 * still outstanding (UserBauble.svelte defers it out of the boot burst), so
 * "Sign in with Google" being visible does not mean `auth.user` is null yet.
 * Clicking inside that window opens the account menu instead of calling
 * `startLogin`. Returns a withhold reason when the engine never answers,
 * rather than proceeding into UI logic that cannot be right yet.
 */
async function waitForAuthSettled(page: Page, budget: Budget): Promise<string | null> {
	const response = await page
		.waitForResponse((candidate) => candidate.url().includes(AUTH_ME_PATH), {
			timeout: budget.waitMs()
		})
		.catch(() => null);
	if (response === null) {
		return `engine did not answer ${AUTH_ME_PATH} before the capture budget expired`;
	}
	if (!response.ok()) {
		return `engine answered ${AUTH_ME_PATH} with HTTP ${response.status()}`;
	}
	return null;
}

async function waitForSignInReady(page: Page, budget: Budget) {
	const signInButton = page.getByRole('button', { name: 'Sign in with Google' });
	const signedInButton = page.getByRole('button', { name: /^Signed in as / });
	await Promise.race([
		signInButton.waitFor({ state: 'visible', timeout: budget.waitMs() }),
		signedInButton.waitFor({ state: 'visible', timeout: budget.waitMs() })
	]);
	if (await signedInButton.isVisible()) {
		await signedInButton.click({ timeout: budget.waitMs() });
		await page
			.getByRole('menuitem', { name: 'Sign out' })
			.click({ timeout: budget.waitMs() });
		await signInButton.waitFor({ state: 'visible', timeout: budget.waitMs() });
	}
	await page.waitForFunction(
		() => {
			const button = document.querySelector(
				'button[aria-label="Sign in with Google"]'
			) as HTMLButtonElement | null;
			return button !== null && !button.disabled;
		},
		{ timeout: budget.waitMs() }
	);
	return signInButton;
}

// ----- Google interstitials ----------------------------------------------

interface GoogleClickOutcome {
	clicked: boolean;
	/** Set when a control was FOUND but its click failed; null otherwise. */
	error: string | null;
}

/** Click `locator`, distinguishing "did not click" from "was not there". */
async function clickGoogleControl(
	locator: Locator,
	label: string
): Promise<GoogleClickOutcome> {
	try {
		await locator.click({ timeout: 5_000 });
		return { clicked: true, error: null };
	} catch (exc) {
		const detail = exc instanceof Error ? exc.message.split('\n')[0] : String(exc);
		return { clicked: false, error: `${label}: ${detail}` };
	}
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
 */
async function clickOneGoogleAffordance(page: Page): Promise<GoogleClickOutcome> {
	const proceed = page.getByRole('button', { name: GOOGLE_PROCEED });
	const proceedCount = await proceed.count().catch(() => 0);
	for (let index = 0; index < proceedCount; index += 1) {
		const button = proceed.nth(index);
		if (!(await button.isVisible().catch(() => false))) continue;
		return clickGoogleControl(button, 'consent button');
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
		return clickGoogleControl(row, 'account row');
	}
	return { clicked: false, error: null };
}

/**
 * Returns the last FAILED click, if any, so the caller can name the real
 * cause instead of the generic "stuck on Google" it would otherwise reach.
 * A click that did not happen is never counted as a round of progress: a
 * blocked or detached control would otherwise burn the round budget while
 * looking like forward motion.
 */
async function driveGoogleInterstitials(page: Page, budget: Budget): Promise<string | null> {
	const deadline = Date.now() + Math.min(budget.remainingMs(), GOOGLE_DRIVE_MAX_MS);
	let lastError: string | null = null;
	for (let round = 0; round < GOOGLE_DRIVE_MAX_ROUNDS; round += 1) {
		if (page.isClosed()) return lastError;
		if (budget.expired() || Date.now() >= deadline) return lastError;
		if (!page.url().includes(GOOGLE_HOST_FRAGMENT)) return null;
		await page
			.waitForLoadState('domcontentloaded', { timeout: Math.min(10_000, budget.waitMs()) })
			.catch(() => null);
		const outcome = await clickOneGoogleAffordance(page);
		if (outcome.error !== null) lastError = outcome.error;
		if (!outcome.clicked) {
			round -= 1;
			if (outcome.error === null) {
				// Nothing actionable on screen yet; wait for it to render.
				await sleep(1_000);
				continue;
			}
			// A control was there and refused the click. Retrying the same
			// control cannot help, so stop and let the caller report it.
			return lastError;
		}
		await sleep(1_500);
	}
	return lastError;
}

// ----- submit marks -------------------------------------------------------

type SubmitMarks =
	| { state: 'read'; hasSubmit: boolean; hasNavigate: boolean }
	| { state: 'corrupt' }
	| { state: 'unreadable' };

/**
 * Read the client's login marks, tolerating the post-login navigation.
 *
 * `page.evaluate` throws "Execution context was destroyed" when the SPA
 * navigates mid-call, which used to crash the capture instead of withholding.
 * This runs on the DIAGNOSTIC path only -- a captured span already proves the
 * marks existed, because `completeLibraryUsable` returns early without them.
 *
 * `corrupt` is deliberately NOT folded into "absent": malformed stored state
 * means the sign-in lifecycle DID run and wrote something, so reporting
 * restored-session there would state the opposite of what happened.
 */
async function readSubmitMarks(page: Page, attempts = 5): Promise<SubmitMarks> {
	for (let attempt = 0; attempt < attempts; attempt += 1) {
		if (page.isClosed()) break;
		try {
			return await page.evaluate((key): SubmitMarks => {
				const raw = sessionStorage.getItem(key);
				if (raw === null) {
					return { state: 'read', hasSubmit: false, hasNavigate: false };
				}
				try {
					const parsed = JSON.parse(raw) as { t0?: number; tNavigate?: number };
					return {
						state: 'read',
						hasSubmit: typeof parsed.t0 === 'number',
						hasNavigate: typeof parsed.tNavigate === 'number'
					};
				} catch {
					return { state: 'corrupt' };
				}
			}, LOGIN_SUBMIT_KEY);
		} catch {
			await sleep(250);
		}
	}
	return { state: 'unreadable' };
}

/**
 * Drop any mark left over from an earlier lifecycle, so the span we accept can
 * only belong to the click this run drives. Returns a reason on failure: a
 * surviving stale mark would let `completeLibraryUsable` post a span timed
 * from the PREVIOUS login, and the capture would score it as this one.
 */
async function clearSubmitMark(page: Page): Promise<string | null> {
	try {
		await page.evaluate((key) => {
			sessionStorage.removeItem(key);
		}, LOGIN_SUBMIT_KEY);
		return null;
	} catch (exc) {
		const detail = exc instanceof Error ? exc.message.split('\n')[0] : String(exc);
		return `cannot isolate this run: ${LOGIN_SUBMIT_KEY} could not be cleared before the click (${detail})`;
	}
}

// ----- proof of submit ----------------------------------------------------

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
async function driveSignInClick(
	page: Page,
	budget: Budget,
	collector: SpanCollector
): Promise<string | null> {
	for (let attempt = 1; attempt <= SIGN_IN_CLICK_ATTEMPTS; attempt += 1) {
		if (budget.expired()) return `capture budget of ${TIMEOUT_S}s expired before the sign-in click`;
		const signInButton = await waitForSignInReady(page, budget);
		const clearReason = await clearSubmitMark(page);
		if (clearReason !== null) return clearReason;
		const waitMs = Math.min(budget.waitMs(), LOGIN_POST_WAIT_MS);
		const isLoginPost = (method: string, url: string): boolean =>
			method === 'POST' && url.includes(AUTH_LOGIN_PATH);
		// The REQUEST proves the click submitted; the RESPONSE proves the
		// daemon accepted it. Reading submission off the response conflates
		// "never submitted" with "submitted and the answer stalled", and the
		// retry below would then fire a SECOND login start for a click that
		// had already worked.
		const loginRequest = page
			.waitForRequest((request) => isLoginPost(request.method(), request.url()), {
				timeout: waitMs
			})
			.catch(() => null);
		const loginResponse = page
			.waitForResponse(
				(response) => isLoginPost(response.request().method(), response.url()),
				{ timeout: waitMs }
			)
			.catch(() => null);
		// Read the gate from the PAGE's clock, the same one `recordPerfSpan`
		// stamps spans with, as the last thing before the click. Every span
		// the page had already built carries an earlier stamp and is refused
		// no matter when Playwright gets around to delivering it.
		collector.openAtClick(await page.evaluate(() => Date.now()));
		// Bounded by the capture budget, not Playwright's default action
		// timeout: a covered or permanently disabled control would otherwise
		// wait until the test deadline, where the teardown happens INSTEAD of
		// the catch block that writes a withheld result.
		await signInButton.click({ timeout: budget.waitMs() });
		const request = await loginRequest;
		if (request === null) {
			// No POST at all: the click hit the bauble in its signed-in state
			// and opened the account menu instead of calling startLogin.
			// Close it and retry -- this is the only retryable case.
			await page.keyboard.press('Escape').catch(() => null);
			continue;
		}
		const response = await loginResponse;
		if (response === null) {
			return `cannot start sign-in: ${AUTH_LOGIN_PATH} was submitted but the daemon did not answer within ${Math.round(waitMs / 1000)}s`;
		}
		if (!response.ok()) {
			return `cannot start sign-in: daemon refused the login start (HTTP ${response.status()})`;
		}
		return null;
	}
	return 'sign-in click did not start a login (the bauble stayed in its signed-in state)';
}

async function ensurePerformance(page: Page, budget: Budget): Promise<void> {
	if (new URL(page.url()).pathname !== '/performance') {
		await page.goto('/performance', { timeout: budget.waitMs() });
	}
}

// ----- capture ------------------------------------------------------------

test('capture S13 login submit-to-library-usable span', async ({ page }) => {
	if (!RESULT_PATH) {
		throw new Error('KPI_CAPTURE_RESULT is required');
	}
	const budget = makeBudget(TIMEOUT_S * 1000);
	try {
		const collector = armLoginSpanCollector(page);

		const authSettled = waitForAuthSettled(page, budget);
		await page.goto('/performance', { timeout: budget.waitMs() });
		const authReason = await authSettled;
		if (authReason !== null) {
			writeResult({ ok: false, span: null, reason: `cannot drive sign-in: ${authReason}` });
			return;
		}

		const navLog = armNavigationLog(page);
		const clickReason = await driveSignInClick(page, budget, collector);
		if (clickReason !== null) {
			writeResult({ ok: false, span: null, reason: clickReason });
			return;
		}

		// The POST proved the login STARTED. This proves the browser actually
		// left, which is what makes a later return to the SPA mean anything.
		const leftTheSpa = await waitForRedirectAway(
			navLog,
			page,
			budget,
			Math.min(budget.remainingMs(), LOGIN_POST_WAIT_MS)
		);
		if (!leftTheSpa) {
			writeResult({
				ok: false,
				span: null,
				reason:
					'cannot complete real Google login: the login started but the browser never navigated away from the SPA (no redirect to the consent URL)'
			});
			return;
		}
		const googleClickError = await driveGoogleInterstitials(page, budget);

		if (budget.expired()) {
			withholdExpired('the login round trip completed');
			return;
		}
		try {
			await page.waitForURL(
				(url) => {
					if (url.origin !== FRONTEND_ORIGIN) return false;
					return url.pathname === '/' || url.pathname === '/performance';
				},
				{ timeout: budget.waitMs() }
			);
		} catch {
			if (googleClickError !== null) {
				writeResult({
					ok: false,
					span: null,
					reason: `cannot complete real Google login: a consent control refused the click (${googleClickError})`
				});
				return;
			}
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

		await ensurePerformance(page, budget);

		const span = await waitForCollectedSpan(page, collector, budget);
		if (span !== null) {
			writeResult({ ok: true, span, reason: null });
			return;
		}

		// A span DID arrive, it just belonged to an earlier login. The
		// lifecycle is healthy; the mark reads below would describe a
		// telemetry fault that did not happen.
		const stale = collector.staleRejected();
		if (stale > 0) {
			writeResult({
				ok: false,
				span: null,
				reason: `stale-span-only: ${stale} login perf-span POST(s) arrived but every one was stamped before this run's sign-in click, so none can be scored as this login`
			});
			return;
		}

		// No span at all. Only now do the marks tell us anything: read them to say WHY.
		const marks = await readSubmitMarks(page);
		if (marks.state === 'unreadable') {
			writeResult({
				ok: false,
				span: null,
				reason:
					'missing-telemetry: no perf-span POST and the page context could not be read to classify it'
			});
			return;
		}
		if (marks.state === 'corrupt') {
			writeResult({
				ok: false,
				span: null,
				reason: `missing-telemetry: no perf-span POST and ${LOGIN_SUBMIT_KEY} held unparseable state (the sign-in lifecycle ran and wrote something)`
			});
			return;
		}
		if (!marks.hasSubmit || !marks.hasNavigate) {
			// NOT restored-session. We only reach here after driveSignInClick
			// observed POST /api/v1/auth/login, which proves startLogin ran and
			// therefore that markLoginSubmit was called. Saying the session was
			// restored without driving Sign in would assert the opposite of
			// something this run has already established.
			const missing = !marks.hasSubmit ? 'submit' : 'navigate';
			writeResult({
				ok: false,
				span: null,
				reason: `missing-telemetry: the login was submitted but the ${missing} mark is absent afterwards (client telemetry lifecycle broken, not a restored session)`
			});
			return;
		}
		writeResult({
			ok: false,
			span: null,
			reason:
				'missing-telemetry: login marks present but no perf-span POST (library-usable hooks absent or library did not reach first paint)'
		});
	} catch (exc) {
		// Every interaction above can throw -- a detached control, a timeout,
		// a closed context -- and an unwritten KPI_CAPTURE_RESULT would leave
		// capture_s13.py guessing from an exit code. The contract is that a
		// capture that cannot measure still says WHY, so say why here too.
		if (!resultWritten) {
			const detail = exc instanceof Error ? exc.message.split('\n')[0] : String(exc);
			writeResult({ ok: false, span: null, reason: `capture aborted before a verdict: ${detail}` });
		}
	}
});
