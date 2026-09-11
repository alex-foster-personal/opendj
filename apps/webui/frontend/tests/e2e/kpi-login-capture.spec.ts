import { test } from '@playwright/test';
import { writeFileSync } from 'node:fs';

const RESULT_PATH = process.env.KPI_CAPTURE_RESULT;
const FRONTEND_ORIGIN = (
	process.env.KPI_CAPTURE_FRONTEND ??
	process.env.PERFORMANCE_E2E_BASE_URL ??
	'http://127.0.0.1:5273'
).replace(/\/$/, '');
const TIMEOUT_S = Number.parseInt(process.env.KPI_CAPTURE_TIMEOUT_S ?? '90', 10);

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

test('capture S13 login submit-to-library-usable span', async ({ page }) => {
	if (!RESULT_PATH) {
		throw new Error('KPI_CAPTURE_RESULT is required');
	}

	const spanPromise = page.waitForRequest(
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
		{ timeout: TIMEOUT_S * 1000 }
	);

	await page.goto('/performance');
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

	if (new URL(page.url()).pathname === '/') {
		await page.goto('/performance');
	}

	let span: Record<string, unknown> | null = null;
	try {
		const request = await spanPromise;
		span = request.postDataJSON() as Record<string, unknown>;
	} catch {
		writeResult({
			ok: false,
			span: null,
			reason: 'missing library-usable mark: no perf-span POST'
		});
		return;
	}

	writeResult({ ok: true, span, reason: null });
});
