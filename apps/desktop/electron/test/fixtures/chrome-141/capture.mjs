// Captures a cookie database written by a real Chromium, for
// test/side-player.test.ts (SPOT-6555c). Run from apps/webui/frontend, which has
// @playwright/test, then re-key it with rekey.mjs (which writes ./Cookies) and
// update MANIFEST.json and the pinned digest in the test to match:
//
//   AF_SERVICE_ID=com.opendj.test.chrome-fixture-capture \
//     node ../../desktop/electron/test/fixtures/chrome-141/capture.mjs <profile-dir> [chromium]
//   CHROMIUM_BASIC_STORE_PASSWORD=<password> \
//     node ../../desktop/electron/test/fixtures/chrome-141/rekey.mjs <profile-dir>/Default/Cookies ../../desktop/electron/test/fixtures/chrome-141/Cookies
//
// --password-store=basic makes Chromium encrypt with the fixed key it ships
// for its Linux basic store: the same v10 AES-128-CBC format and DB-24 host
// binding the macOS importer reads. That key is never written in this
// repository (research rule R7); rekey.mjs explains. Every value is a fixture
// value, never a real login.
import { chromium } from '@playwright/test';

const [profileDir, executablePath = '/opt/pw-browsers/chromium'] = process.argv.slice(2);
if (!profileDir) throw new Error('usage: capture.mjs <profile-dir> [chromium]');
// The browser this spawns carries the owner tag, so the orphan reaper can
// attribute it if this script dies before closing it.
if (!process.env.AF_SERVICE_ID) throw new Error('owned browser processes require AF_SERVICE_ID');
const ctx = await chromium.launchPersistentContext(profileDir, {
	executablePath,
	headless: true,
	env: { ...process.env, AF_SERVICE_ID: process.env.AF_SERVICE_ID },
	args: ['--password-store=basic', '--no-sandbox', '--hide-crash-restore-bubble']
});
try {
	const expires = Math.floor(Date.UTC(2099, 0, 1) / 1000); // Chromium caps this at 400 days
	await ctx.addCookies([
		{ name: 'sp_dc', value: 'fixture-spotify-value', domain: '.spotify.com', path: '/', expires, httpOnly: true, secure: true, sameSite: 'Lax' },
		{ name: 'host_only', value: 'fixture-host-only', url: 'https://open.spotify.com/', expires, httpOnly: false, secure: true, sameSite: 'None' },
		{ name: 'session', value: 'fixture-beatport-value', domain: '.beatport.com', path: '/', expires, httpOnly: true, secure: true, sameSite: 'Strict' },
		{ name: 'SID', value: 'fixture-google-value', domain: '.google.com', path: '/', expires, httpOnly: true, secure: true, sameSite: 'Lax' }
	]);
	const page = await ctx.newPage();
	await page.goto('about:blank');
	await new Promise((resolve) => setTimeout(resolve, 2000)); // let the cookie store flush
} finally {
	await ctx.close();
}
