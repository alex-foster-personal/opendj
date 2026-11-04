// What the page may ask the shell to open in the system browser.
//
// Port of apps/desktop/src-tauri/capabilities/opener.json, the scope of the
// `opendj:open-external` bridge call (the Tauri `openUrl`). It is narrower than
// link handling: a link the user clicks (`target="_blank"`, `window.open`) goes
// through classifyMainFrameNavigation and may open any https page, but a URL
// the page hands to the bridge must be one of these (parity matrix row 35):
//
//   - the named local origin http://opendj.localhost:<4-5 digit port>/...
//     (Settings > Open in browser),
//   - the Google consent URL https://accounts.google.com/o/oauth2/v2/auth?...
//     (system-browser sign-in),
//   - exactly https://existential.audio/blackhole/ (the BlackHole installer),
//   - an https page on polar.sh or a subdomain of it, default port: Polar
//     checkout and the customer portal (lib/account/hosted-billing-api.ts
//     `polarUrl` applies the same host rule before calling the bridge).
//
// Userinfo is refused everywhere, so a URL whose real host follows a userinfo
// part shaped like an allowed origin (the named local port, polar.sh) is out.

import { isOpenDjLocalhostUrl } from './policy';

const GOOGLE_CONSENT_PREFIX = 'https://accounts.google.com/o/oauth2/v2/auth?';
const BLACKHOLE_INSTALLER_URL = 'https://existential.audio/blackhole/';
const POLAR_HOST = 'polar.sh';

function isGoogleConsentUrl(url: string, parsed: URL): boolean {
	return (
		url.startsWith(GOOGLE_CONSENT_PREFIX) &&
		parsed.protocol === 'https:' &&
		parsed.hostname === 'accounts.google.com' &&
		parsed.port === '' &&
		parsed.pathname === '/o/oauth2/v2/auth'
	);
}

function isPolarBillingUrl(parsed: URL): boolean {
	const host = parsed.hostname;
	return parsed.protocol === 'https:' && parsed.port === '' && (host === POLAR_HOST || host.endsWith(`.${POLAR_HOST}`));
}

/** May the page's `openExternal` bridge call open `url` in the system browser? Never throws. */
export function isOpenerUrlAllowed(url: string): boolean {
	if (url === BLACKHOLE_INSTALLER_URL) return true;
	let parsed: URL;
	try {
		parsed = new URL(url);
	} catch {
		return false;
	}
	if (parsed.username !== '' || parsed.password !== '') return false;
	return isOpenDjLocalhostUrl(url, parsed) || isGoogleConsentUrl(url, parsed) || isPolarBillingUrl(parsed);
}
