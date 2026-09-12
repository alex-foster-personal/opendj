import { test, type Page } from '@playwright/test';
import { writeFileSync } from 'node:fs';

const RESULT_PATH = process.env.KPI_CAPTURE_RESULT;
const FRONTEND_ORIGIN = (
	process.env.KPI_CAPTURE_FRONTEND ??
	process.env.PERFORMANCE_E2E_BASE_URL ??
	'http://127.0.0.1:5273'
).replace(/\/$/, '');
const TIMEOUT_S = Number.parseInt(process.env.KPI_CAPTURE_TIMEOUT_S ?? '90', 10);
const LOGIN_SUBMIT_KEY = 'mdt.loginSubmitAt';

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

async function waitForSignInReady(page: Page) {
	const signInButton = page.getByRole('button', { name: 'Sign in with Google' });
	const signedInButton = page.getByRole('button', { name: /^Signed in as / });
	await Promise.race([
		signInButton.waitFor({ state: 'visible', timeout: 30_000 }),
		signedInButton.waitFor({ state: 'visible', timeout: 30_000 })
	]);
	if (await signedInButton.isVisible()) {
		await signedInButton.click();
		await page.getByRole('menuitem', { name: 'Sign out' }).click();
		await signInButton.waitFor({ state: 'visible', timeout: 30_000 });
	}
	await page.waitForFunction(
		() => {
			const button = document.querySelector(
				'button[aria-label="Sign in with Google"]'
			) as HTMLButtonElement | null;
			return button !== null && !button.disabled;
		},
		{ timeout: 30_000 }
	);
	return signInButton;
}

async function ensurePerformance(page: Page): Promise<void> {
	if (new URL(page.url()).pathname !== '/performance') {
		await page.goto('/performance');
	}
}

async function readSubmitMarks(
	page: Page
): Promise<{ hasSubmit: boolean; hasNavigate: boolean }> {
	return page.evaluate((key) => {
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
}

async function waitForLoginSpan(
	page: Page,
	timeoutMs: number
): Promise<Record<string, unknown>> {
	const request = await page.waitForRequest(
		(request) => {
			if (request.method() !== 'POST') return false;
			if (!request.url().includes('/api/v1/client-events')) return false;
			try {
				const body = request.postDataJSON() as Record<string, unknown>;
				return isLoginPerfSpan(body);
			} catch {
				return false;
			}
		},
		{ timeout: timeoutMs }
	);
	return request.postDataJSON() as Record<string, unknown>;
}

test('capture S13 login submit-to-library-usable span', async ({ page }) => {
	if (!RESULT_PATH) {
		throw new Error('KPI_CAPTURE_RESULT is required');
	}

	await page.goto('/performance');
	const signInButton = await waitForSignInReady(page);
	await signInButton.click();

	try {
		await page.waitForURL(
			(url) => {
				if (url.origin !== FRONTEND_ORIGIN) return false;
				return url.pathname === '/' || url.pathname === '/performance';
			},
			{ timeout: TIMEOUT_S * 1000 }
		);
	} catch {
		const host = new URL(page.url()).hostname;
		if (host.includes('google.')) {
			const title = await page.title();
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

	const marks = await readSubmitMarks(page);
	if (!marks.hasSubmit || !marks.hasNavigate) {
		writeResult({
			ok: false,
			span: null,
			reason:
				'restored-session: submit or navigate mark missing after login (session was restored without driving Sign in)'
		});
		return;
	}

	try {
		const span = await waitForLoginSpan(page, TIMEOUT_S * 1000);
		writeResult({ ok: true, span, reason: null });
	} catch {
		writeResult({
			ok: false,
			span: null,
			reason:
				'missing-telemetry: login marks present but no perf-span POST (library-usable hooks absent or library did not reach first paint)'
		});
	}
});
