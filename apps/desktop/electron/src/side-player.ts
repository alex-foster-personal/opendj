// The Spotify side player (SPOT-6555b): open.spotify.com docked to the right of
// the Open DJ window, so a DJ gets a Spotify remote without a whole Chrome.
//
// Thin-shell rule: this owns a window and nothing else. It decides no product
// behavior; the page asks it to open a URL, collapse, close or import sign-ins.
//
// Shape: one frameless child window of the main window. Its left STRIP_W
// pixels are a local strip (click to collapse or expand); the rest is the
// Spotify web player in its own persistent session, with no preload and no
// bridge, so the Spotify page can never call into the shell. Collapsed, the
// window is just the strip.
//
// Playback note: stock Electron has no Widevine, so the web player cannot
// play audio itself. It works as a Spotify Connect remote (pick the phone or
// the desktop app as the device), which is what the maintainer asked for. Local audio
// needs a Widevine build of Electron (castLabs ECS); see the PR.

import { BrowserWindow, WebContentsView, app, dialog, screen, session, shell, type Rectangle } from 'electron';

import {
	type ImportOutcome,
	storeImportedCookies,
	LOGIN_SITES,
	chromeKeyFromPassword,
	chromeProfileCookies,
	chromeSafeStoragePassword,
	importConsentText,
	readChromeLoginCookies,
	unsupportedPlatformReason
} from './chrome-logins';
import { appendShellLog } from './shell-log';
import {
	STRIP_W,
	closedMainWidth,
	isSidePlayerOpenTarget,
	isSidePlayerUrl,
	plainChromeUserAgent,
	sideDockUnavailableReason,
	sidePlayerLayout
} from './side-player-layout';

/** One session for the player (and later the store windows) so a sign-in sticks. */
export const SIDE_PARTITION = 'persist:opendj-side-browser';

const STRIP_SCHEME = 'opendj-side:';

function stripHtml(collapsed: boolean): string {
	const arrow = collapsed ? '&#x2039;' : '&#x203A;';
	const label = collapsed ? 'Show Spotify' : 'Hide Spotify';
	return `data:text/html;charset=utf-8,${encodeURIComponent(`<!doctype html><html><head><style>
html,body{margin:0;height:100%;background:#121212;overflow:hidden}
a{display:flex;align-items:center;justify-content:center;height:100%;color:#1ed760;
font:bold 11px -apple-system,system-ui,sans-serif;text-decoration:none;border-left:1px solid #2a2a2a;box-sizing:border-box}
a:hover{background:#1f1f1f}
</style></head><body><a href="opendj-side://toggle" title="${label}" aria-label="${label}">${arrow}</a></body></html>`)}`;
}

export class SidePlayer {
	private win: BrowserWindow | null = null;
	private spotify: WebContentsView | null = null;
	private strip: WebContentsView | null = null;
	private collapsed = false;
	private restoreWidth: number | null = null;
	private placing = false;
	private stripShows: boolean | null = null;
	/** Settings: "Shrink Open DJ to make room for the Spotify player"; the page sends it with each open. */
	private shrinkMain = true;
	/** Bumped by every open(); a superseded open neither closes nor shows the player. */
	private openGeneration = 0;

	constructor(private readonly main: BrowserWindow) {
		const follow = (): void => {
			if (!this.placing) this.place();
		};
		main.on('move', follow);
		main.on('resize', follow);
		main.on('closed', () => this.close());
	}

	isOpen(): boolean {
		return this.win !== null && !this.win.isDestroyed();
	}

	async open(url: string, options: { shrinkMain?: boolean } = {}): Promise<void> {
		if (!isSidePlayerOpenTarget(url)) throw new Error(`side player refuses ${url}`);
		const undockable = sideDockUnavailableReason(
			process.platform,
			process.env,
			app.commandLine.getSwitchValue('ozone-platform'),
			app.commandLine.getSwitchValue('ozone-platform-hint')
		);
		if (undockable !== null) throw new Error(undockable);
		if (typeof options.shrinkMain === 'boolean') this.shrinkMain = options.shrinkMain;
		this.ensureWindow();
		this.collapsed = false;
		this.place();
		// A newer open aborts this navigation (ERR_ABORTED); only the latest
		// open may decide that the player failed.
		const generation = ++this.openGeneration;
		try {
			await this.spotify?.webContents.loadURL(url);
		} catch (error) {
			if (generation !== this.openGeneration) return;
			// Never leave Open DJ shrunk beside a player that never appeared.
			this.close();
			throw error;
		}
		if (generation !== this.openGeneration) return;
		this.win?.showInactive();
		appendShellLog('side-player', `opened ${new URL(url).pathname}`);
	}

	setCollapsed(collapsed: boolean): void {
		if (!this.isOpen()) return;
		this.collapsed = collapsed;
		this.place();
	}

	close(): void {
		const win = this.win;
		const views = [this.spotify, this.strip];
		this.win = null;
		this.spotify = null;
		this.strip = null;
		this.stripShows = null;
		// Views added through contentView are not closed with their window
		// (Electron's resource management): end their renderers explicitly.
		for (const view of views) {
			if (view !== null && !view.webContents.isDestroyed()) view.webContents.close();
		}
		if (win !== null && !win.isDestroyed()) win.destroy();
		// Give Open DJ back the width it lent.
		if (this.restoreWidth !== null && !this.main.isDestroyed()) {
			const bounds = this.main.getBounds();
			const workArea = screen.getDisplayMatching(bounds).workArea;
			this.placing = true;
			this.main.setBounds({ ...bounds, width: closedMainWidth(bounds, workArea, this.restoreWidth) });
			this.placing = false;
		}
		this.restoreWidth = null;
	}

	/** Copy the listed sites' Chrome sign-ins into the side player's session, after asking. */
	async importBrowserLogins(): Promise<ImportOutcome> {
		const unsupported = unsupportedPlatformReason(process.platform);
		if (unsupported !== null) return { kind: 'refused', reason: unsupported };
		const profile = chromeProfileCookies();
		if (profile.kind === 'refused') return profile;
		const cookiesDb = profile.cookies;
		const choice = await dialog.showMessageBox(this.main, {
			type: 'question',
			title: 'Import sign-ins from Chrome',
			...importConsentText(LOGIN_SITES),
			buttons: ['Import', 'Cancel'],
			defaultId: 0,
			cancelId: 1
		});
		if (choice.response !== 0) return { kind: 'refused', reason: 'Cancelled.' };
		let password: string;
		try {
			password = await chromeSafeStoragePassword();
		} catch (error) {
			return { kind: 'refused', reason: String(error instanceof Error ? error.message : error) };
		}
		const result = readChromeLoginCookies(cookiesDb, chromeKeyFromPassword(password));
		const target = session.fromPartition(SIDE_PARTITION);
		const outcome = await storeImportedCookies(target.cookies, result);
		appendShellLog(outcome.kind === 'imported' ? 'side-player' : 'WARN', `side player import: ${JSON.stringify(outcome)} (${result.skipped} skipped)`);
		if (this.isOpen()) this.spotify?.webContents.reload();
		return outcome;
	}

	private ensureWindow(): void {
		if (this.isOpen()) return;
		const win = new BrowserWindow({
			parent: this.main,
			frame: false,
			show: false,
			resizable: false,
			minimizable: false,
			maximizable: false,
			fullscreenable: false,
			skipTaskbar: true,
			backgroundColor: '#121212',
			title: 'Spotify'
		});
		const spotify = new WebContentsView({
			webPreferences: {
				partition: SIDE_PARTITION,
				contextIsolation: true,
				sandbox: true,
				nodeIntegration: false,
				webSecurity: true
			}
		});
		const strip = new WebContentsView({
			webPreferences: { contextIsolation: true, sandbox: true, nodeIntegration: false, javascript: false }
		});
		// Spotify's sign-in rejects Electron's default UA as an unsupported browser.
		spotify.webContents.setUserAgent(plainChromeUserAgent(app.userAgentFallback));
		win.contentView.addChildView(spotify);
		win.contentView.addChildView(strip);

		// The Spotify page stays on Spotify; anything else goes to the real browser.
		const guard = (event: { preventDefault: () => void }, url: string): void => {
			if (isSidePlayerUrl(url)) return;
			event.preventDefault();
			if (url.startsWith('https://')) void shell.openExternal(url);
		};
		spotify.webContents.on('will-navigate', guard);
		spotify.webContents.on('will-redirect', guard);
		spotify.webContents.setWindowOpenHandler(({ url }) => {
			if (url.startsWith('https://')) void shell.openExternal(url);
			return { action: 'deny' };
		});
		const sideSession = session.fromPartition(SIDE_PARTITION);
		sideSession.setPermissionRequestHandler((_contents, permission, callback) => {
			// Spotify asks for protected media and notifications; neither is granted.
			appendShellLog('side-player', `refused permission ${permission}`);
			callback(false);
		});
		// Checks (the Permissions API, service-worker notifications) take a
		// separate path; deny those too so the policy covers both.
		sideSession.setPermissionCheckHandler(() => false);

		strip.webContents.on('will-navigate', (event, url) => {
			event.preventDefault();
			if (url.startsWith(STRIP_SCHEME)) this.setCollapsed(!this.collapsed);
		});

		win.on('closed', () => {
			if (this.win === win) this.close();
		});
		this.win = win;
		this.spotify = spotify;
		this.strip = strip;
	}

	private place(): void {
		if (!this.isOpen() || this.main.isDestroyed()) return;
		const mainBounds = this.main.getBounds();
		const workArea = screen.getDisplayMatching(mainBounds).workArea;
		const layout = sidePlayerLayout({
			main: mainBounds,
			workArea,
			collapsed: this.collapsed,
			shrinkMain: this.shrinkMain,
			restoreWidth: this.restoreWidth
		});
		this.restoreWidth = layout.restoreWidth;
		const same = (a: Rectangle, b: Rectangle): boolean =>
			a.x === b.x && a.y === b.y && a.width === b.width && a.height === b.height;
		if (!same(layout.main, mainBounds)) {
			this.placing = true;
			this.main.setBounds(layout.main);
			this.placing = false;
		}
		this.win?.setBounds(layout.player);
		const { width, height } = layout.player;
		this.strip?.setBounds({ x: 0, y: 0, width: STRIP_W, height });
		if (this.stripShows !== this.collapsed) {
			this.stripShows = this.collapsed;
			void this.strip?.webContents.loadURL(stripHtml(this.collapsed));
		}
		this.spotify?.setVisible(!this.collapsed);
		this.spotify?.setBounds({ x: STRIP_W, y: 0, width: Math.max(0, width - STRIP_W), height });
	}
}
