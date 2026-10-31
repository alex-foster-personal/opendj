/**
 * Settings > "Open in browser" (UIURL-03): what the control shows, and what a
 * click does, for every place this page can be running.
 *
 * The engine serves the whole UI, so a Chrome tab and the desktop window are
 * the same engine and the same library. The address comes from the engine
 * itself (GET /api/v1/ui-url), never from this page's own location: inside
 * the app the page is on http://127.0.0.1:<port>, and the address worth
 * handing a person is the named one, http://opendj.localhost:<port>/.
 *
 * - In a desktop shell (Tauri or Electron) the button opens that address in
 *   the default browser through the shell.
 * - In a browser tab, opening another tab of the same page is pointless, so
 *   the control shows the address with a Copy button instead.
 * - On a page reached by any non-local host (a share tunnel), the engine's
 *   local address is not one this viewer can use, so the control is hidden.
 *
 * Pure on purpose: the view is a function of four inputs, so every state is
 * under node:test rather than under a component mount.
 *
 * Requirements (mini-PRD):
 *   ✔︎ a shell page with an address shows an enabled "Open in browser".
 *     [if] a Tauri or Electron page with an address renders anything but 'open' [then ⛔️] broken
 *   ✔︎ a browser tab shows the address and Copy, never an open button.
 *     [if] a browser tab on loopback renders 'open' [then ⛔️] broken
 *   ✔︎ no address yet or a refusal renders inert with a hover reason.
 *     [if] a failed /ui-url read renders an enabled control [then ⛔️] broken
 */
import type { NativeShellKind } from '$lib/shell/native-shell';

export const OPEN_IN_BROWSER_LABEL = 'Open in browser';

export const OPEN_IN_BROWSER_TITLE =
	'Opens Open DJ in your default browser. The browser tab and this app window ' +
	'are the same engine and the same library: a change in one shows in the other.';

export const COPY_ADDRESS_TITLE =
	'This tab is already in a browser. Copy this address to open the same library ' +
	'in another browser or profile; it is the same engine and library as the app window.';

export const NAMED_LOCAL_HOST = 'opendj.localhost';

/** Hostnames a browser on this machine reaches the engine by. */
const LOCAL_PAGE_HOSTNAMES: ReadonlySet<string> = new Set([
	'127.0.0.1',
	'localhost',
	'[::1]',
	'::1',
	NAMED_LOCAL_HOST
]);

/** The GET /api/v1/ui-url body this module reads. */
export interface UiAddress {
	ui_url: string;
	loopback_url: string;
	port: number;
	served_by: 'engine' | 'dev-frontend';
}

export interface OpenInBrowserInput {
	address: UiAddress | null;
	/** Why the address read failed, or null when it has not failed. */
	error: string | null;
	shell: NativeShellKind | null;
	pageHostname: string;
}

export type OpenInBrowserView =
	| { kind: 'loading'; title: string }
	| { kind: 'unavailable'; title: string }
	| { kind: 'open'; url: string; title: string }
	| { kind: 'copy'; url: string; title: string }
	| { kind: 'hidden' };

/** The engine's address must parse to http on exactly opendj.localhost, so a
 * malformed or smuggled value (userinfo, lookalike host) is never opened. */
export function isNamedLocalUrl(url: string): boolean {
	try {
		const parsed = new URL(url);
		return (
			parsed.protocol === 'http:' &&
			parsed.hostname === NAMED_LOCAL_HOST &&
			parsed.username === '' &&
			parsed.password === '' &&
			parsed.port !== ''
		);
	} catch {
		return false;
	}
}

export function openInBrowserView(input: OpenInBrowserInput): OpenInBrowserView {
	if (input.shell === null && !LOCAL_PAGE_HOSTNAMES.has(input.pageHostname)) {
		return { kind: 'hidden' };
	}
	if (input.error !== null) {
		return { kind: 'unavailable', title: `No browser address: ${input.error}` };
	}
	if (input.address === null) {
		return { kind: 'loading', title: 'Asking the engine for its browser address...' };
	}
	if (!isNamedLocalUrl(input.address.ui_url)) {
		return { kind: 'unavailable', title: `Refusing an address off the named local host: ${input.address.ui_url}` };
	}
	if (input.shell !== null) {
		return { kind: 'open', url: input.address.ui_url, title: OPEN_IN_BROWSER_TITLE };
	}
	return { kind: 'copy', url: input.address.ui_url, title: COPY_ADDRESS_TITLE };
}
