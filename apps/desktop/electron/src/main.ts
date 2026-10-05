// Open DJ desktop shell on Electron: entry point.
//
// THIN SHELL WITH A LIFECYCLE (ADR-0048, apps/desktop/README.md). This process
// owns no application logic: no product decisions, no data access, no HTTP
// client for the API. It starts the engine that ships inside the app, keeps it
// alive, opens a Chromium window onto it, and offers the page four native
// features. Everything a user sees is served by the engine over HTTP.
//
// Port of apps/desktop/src-tauri/src/main.rs. Behaviors are numbered P1-P19 in
// .planning/phases/20-electron-desktop-shell/20-CONTEXT.md.

import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { pathToFileURL } from 'node:url';

import {
	BrowserWindow,
	app,
	dialog,
	ipcMain,
	net,
	protocol,
	screen,
	session,
	shell,
	type IpcMainEvent,
	type IpcMainInvokeEvent
} from 'electron';

import { defaultBootDeps, startEngine } from './boot';
import { PAYLOAD_DIR } from './engine';
import { makeLockProbe } from './launch';
import { outputHealthJson } from './output-health';
import {
	APP_HOST,
	APP_ORIGIN,
	APP_SCHEME,
	type PageInit,
	diagnosticOutput,
	isAppPage,
	isExternalUrlAllowed,
	isTrustedPage,
	permissionAllowed,
	readBuildStamp,
	shellBuildIdentity,
	startingWindowSize
} from './policy';
import { ShellHealthServer } from './shell-health';
import { EngineError, appendShellLog, installShellLogging } from './shell-log';
import { EngineSupervisor, type SupervisorSurface, defaultDeps, supervisedOrigin } from './supervisor';
import { applyUpdate } from './updater';

/** Test seam, as in the Tauri shell: drive the app against an engine someone else started. */
const ENGINE_ORIGIN_ENV = 'OPENDJ_ENGINE_ORIGIN';
const ENGINE_LOG = path.join('logs', 'engine.log');
/** The bundle identifier the Tauri build uses; keeps the same library folder (P9, rule 2). */
const DEFAULT_IDENTIFIER = 'com.opendj.desktop';

// ----- P1: diagnostic flags, before anything touches the disk ----------------
const argv = process.argv.slice(app.isPackaged ? 1 : 2);
const diagnostic = diagnosticOutput(argv, app.getVersion());
if (diagnostic !== null) {
	process.stdout.write(diagnostic);
	app.exit(0);
} else {
	main();
}

function readIdentifier(): string {
	try {
		const pkg = JSON.parse(fs.readFileSync(path.join(app.getAppPath(), 'package.json'), 'utf8')) as {
			opendj?: { identifier?: unknown };
		};
		const id = pkg.opendj?.identifier;
		if (typeof id === 'string' && /^[A-Za-z0-9.-]+$/.test(id)) return id;
	} catch {
		// Fall through to the default.
	}
	return DEFAULT_IDENTIFIER;
}

/**
 * The engine's data dir: where Tauri's `app_data_dir()` puts it, so switching
 * shells keeps the user's library. Unpackaged builds accept
 * OPENDJ_SHELL_DATA_DIR so tests never touch a real library.
 */
function engineDataDir(identifier: string): string {
	const override = process.env.OPENDJ_SHELL_DATA_DIR;
	if (!app.isPackaged && override !== undefined && override.trim() !== '') return path.resolve(override);
	if (process.platform === 'linux') {
		const base = process.env.XDG_DATA_HOME?.trim() || path.join(os.homedir(), '.local', 'share');
		return path.join(base, identifier);
	}
	return path.join(app.getPath('appData'), identifier);
}

function payloadDir(): string {
	const override = process.env.OPENDJ_PAYLOAD_DIR;
	if (!app.isPackaged && override !== undefined && override.trim() !== '') return path.resolve(override);
	return app.isPackaged ? path.join(process.resourcesPath, PAYLOAD_DIR) : path.join(app.getAppPath(), PAYLOAD_DIR);
}

/** The bootstrap page (apps/desktop/setup), shared with the Tauri shell. */
function setupDir(): string {
	return app.isPackaged ? path.join(process.resourcesPath, 'setup') : path.resolve(app.getAppPath(), '..', 'setup');
}

function failVisibly(error: unknown): never {
	const headline = error instanceof EngineError ? error.headline : 'Open DJ cannot start.';
	const detail = error instanceof EngineError ? error.detail : String(error instanceof Error ? error.message : error);
	appendShellLog('ERROR', `${headline}\n\n${detail}`);
	dialog.showErrorBox('Open DJ cannot start', `${headline}\n\n${detail}`);
	app.exit(1);
	throw error;
}

function main(): void {
	// P19/D10: a CDP port only for unpackaged builds; packaged builds also have
	// the inspect fuses off, so this surface cannot ship.
	const cdpPort = process.env.OPENDJ_REMOTE_DEBUGGING_PORT;
	if (!app.isPackaged && cdpPort !== undefined && /^\d{1,5}$/.test(cdpPort)) {
		app.commandLine.appendSwitch('remote-debugging-port', cdpPort);
		app.commandLine.appendSwitch('remote-debugging-address', '127.0.0.1');
	}

	protocol.registerSchemesAsPrivileged([
		{ scheme: APP_SCHEME, privileges: { standard: true, supportFetchAPI: true, codeCache: true } }
	]);

	// P2: single instance, claimed before any launch plan can run (#2868).
	if (!app.requestSingleInstanceLock()) {
		app.exit(0);
		return;
	}

	const identifier = readIdentifier();
	const productName = app.getName();
	const stamp = readBuildStamp(path.join(__dirname, 'build-identity.json'));
	const identity = shellBuildIdentity(app.getVersion(), stamp);

	let window: BrowserWindow | null = null;
	let supervisor: EngineSupervisor | null = null;
	let quitAllowed = false;
	let shuttingDown: Promise<void> | null = null;
	const page: PageInit = { engineOrigin: null, shellBuild: identity, supervisor: null };

	const shutdownAll = (reason: string): Promise<void> => {
		if (shuttingDown === null) {
			shuttingDown = (async () => {
				appendShellLog('shutdown', `shell exit: ${reason}`);
				if (supervisor !== null) {
					appendShellLog('shutdown', 'shell exit: stopping runtime supervisor');
					await supervisor.shutdown();
					appendShellLog('shutdown', 'shell exit: runtime supervisor stopped');
				} else {
					appendShellLog('shutdown', 'shell exit: no runtime supervisor (external engine origin)');
				}
			})();
		}
		return shuttingDown;
	};

	const exitApp = (code: number, reason: string): void => {
		quitAllowed = true;
		void shutdownAll(reason).finally(() => app.exit(code));
	};

	// Signals from a terminal or a script: stop the engine, then go (P10).
	// Installed after ready (below): Chromium installs its own shutdown handler
	// during startup, which turns SIGTERM into a gated quit, so a handler
	// registered before it never runs. Measured under Xvfb, Thu 24 Sep 2026.
	const installSignalHandlers = (): void => {
		for (const signal of ['SIGTERM', 'SIGINT'] as const) {
			process.once(signal, () => {
				appendShellLog('shutdown', `shell exiting: received ${signal}`);
				exitApp(0, `received ${signal}`);
			});
		}
	};

	app.on('second-instance', () => {
		if (window === null) {
			appendShellLog('WARN', 'second launch detected but no existing window to focus');
			return;
		}
		if (window.isMinimized()) window.restore();
		window.show();
		window.focus();
		appendShellLog('single-instance', 'second launch focused the existing window');
	});

	/** P16: quit decisions belong to the engine-served UI (INSTALL-21). */
	const requestQuitFromWebview = (trigger: string): void => {
		appendShellLog('shutdown', `exit requested: ${trigger}`);
		const current = window?.webContents.getURL() ?? '';
		if (window === null || !isTrustedPage(current) || isAppPage(current)) {
			// The bootstrap and fatal pages have no quit gate and nothing to lose.
			exitApp(0, `${trigger} on the bootstrap page`);
			return;
		}
		window.webContents.executeJavaScript('globalThis.__OPENDJ_requestQuit?.()').catch((error: unknown) => {
			appendShellLog('WARN', `quit hook failed (${String(error)}); quitting directly`);
			exitApp(0, `${trigger} with a failed quit hook`);
		});
	};

	app.on('before-quit', (event) => {
		if (quitAllowed) return;
		event.preventDefault();
		requestQuitFromWebview('apple-event-quit');
	});
	app.on('will-quit', (event) => {
		if (shuttingDown === null) {
			event.preventDefault();
			exitApp(0, 'will-quit');
		}
	});
	app.on('window-all-closed', () => exitApp(0, 'all windows closed'));

	// ----- the four native features, origin-checked (D4) --------------------
	const trusted = (event: IpcMainEvent | IpcMainInvokeEvent): boolean => {
		const url = event.senderFrame?.url ?? '';
		if (event.sender !== window?.webContents || !isTrustedPage(url)) {
			// A new window's first document is an empty about:blank that loads the
			// preload too; refusing it is routine, not worth a warning.
			if (url !== '' && url !== 'about:blank') appendShellLog('WARN', `refused native bridge call from ${url}`);
			return false;
		}
		return true;
	};
	const refuse = (): never => {
		throw new Error('this page is not allowed to use the Open DJ shell bridge');
	};

	ipcMain.on('opendj:page-init', (event) => {
		event.returnValue = trusted(event) ? page : null;
	});
	ipcMain.handle('opendj:pick-folder', async (event, options: { title?: unknown } | undefined) => {
		if (!trusted(event) || window === null) return refuse();
		const title = typeof options?.title === 'string' ? options.title : 'Choose a folder';
		const result = await dialog.showOpenDialog(window, { title, properties: ['openDirectory', 'createDirectory'] });
		return result.canceled ? null : (result.filePaths[0] ?? null);
	});
	ipcMain.handle('opendj:open-external', async (event, url: unknown) => {
		if (!trusted(event)) return refuse();
		if (typeof url !== 'string' || !isExternalUrlAllowed(url)) throw new Error(`refusing to open ${String(url)}`);
		await shell.openExternal(url);
	});
	ipcMain.handle('opendj:exit', (event, code: unknown) => {
		if (!trusted(event)) return refuse();
		const exitCode = Number.isInteger(code) ? (code as number) : 0;
		exitApp(exitCode, `programmatic-exit(code=${exitCode})`);
	});
	ipcMain.handle('opendj:relaunch', (event) => {
		if (!trusted(event)) return refuse();
		app.relaunch();
		exitApp(0, 'relaunch requested');
	});
	ipcMain.handle('opendj:update-apply', async (event) => {
		if (!trusted(event)) return refuse();
		return applyUpdate(
			{
				isPackaged: app.isPackaged,
				currentVersion: app.getVersion(),
				allowQuit: () => {
					quitAllowed = true;
				}
			},
			(progress) => event.sender.send('opendj:update-progress', progress)
		);
	});

	void app.whenReady().then(async () => {
		installSignalHandlers();
		const dataDir = engineDataDir(identifier);
		const logPath = path.join(dataDir, ENGINE_LOG);
		try {
			fs.mkdirSync(dataDir, { recursive: true });
			installShellLogging(logPath);
		} catch (error) {
			failVisibly(error);
		}
		appendShellLog('INFO', `electron shell ${app.getVersion()} (electron ${process.versions.electron}) data dir ${dataDir}`);

		// The bootstrap page, served like tauri://localhost was.
		const setupRoot = setupDir();
		protocol.handle(APP_SCHEME, (request) => {
			const url = new URL(request.url);
			const relative = decodeURIComponent(url.pathname).replace(/^\/+/, '') || 'index.html';
			const target = path.resolve(setupRoot, relative);
			if (url.host !== APP_HOST || !target.startsWith(setupRoot + path.sep)) {
				return new Response('not found', { status: 404 });
			}
			return net.fetch(pathToFileURL(target).toString());
		});

		// Permissions: trusted loopback pages, a short list (D4).
		const ses = session.defaultSession;
		ses.setPermissionRequestHandler((_contents, permission, callback, details) => {
			const wantsVideo = permission === 'media' && (details as { mediaTypes?: string[] }).mediaTypes?.includes('video');
			callback(!wantsVideo && permissionAllowed(permission, details.requestingUrl));
		});
		ses.setPermissionCheckHandler((_contents, permission, requestingOrigin) => permissionAllowed(permission, requestingOrigin));

		// P3-P8 / P5: start or adopt the engine, or honor the external origin.
		const payload = payloadDir();
		const lockProbe = makeLockProbe(path.join(payload, 'runtime', 'bin', 'python3'));
		const external = process.env[ENGINE_ORIGIN_ENV];
		let health: ShellHealthServer | null = null;
		try {
			if (external !== undefined) {
				if (external.trim() === '') {
					throw new EngineError(
						`${ENGINE_ORIGIN_ENV} is set but empty.`,
						'Give a full loopback origin including the port, or unset it to let the app start its own bundled engine.'
					);
				}
				page.engineOrigin = external.trim();
			} else {
				const bootDeps = defaultBootDeps(lockProbe, async (pid, detail, lock) => {
					const choice = await dialog.showMessageBox({
						type: 'error',
						title: 'Open DJ cannot start',
						message: `Another Open DJ engine (pid ${pid}) already holds the lock at ${lock}.`,
						detail: `${detail}\n\nStop that engine and start a new one, or quit.`,
						buttons: ['Stop engine', 'Quit'],
						defaultId: 0,
						cancelId: 1
					});
					return choice.response === 0;
				});
				const started = await startEngine(payload, dataDir, logPath, bootDeps);
				page.engineOrigin = supervisedOrigin(started);
				health = await ShellHealthServer.start(dataDir, (action) => outputHealthJson(action, payload));
				const surface: SupervisorSurface = {
					setTitle: (title) => window?.setTitle(title),
					navigateToOrigin: (origin) => {
						page.engineOrigin = origin;
						page.supervisor = null;
						void window?.loadURL(`${origin}/performance`);
					},
					navigateFatal: ({ exitCode, pid, port, healthPort }) => {
						page.supervisor = { engine: 'dead', exit_code: exitCode, lock_pid: pid, lock_port: port, health_port: healthPort };
						void window?.loadURL(`${APP_ORIGIN}/index.html?fatal=1&exit=${exitCode}&pid=${pid}&port=${port}&health=${healthPort}`);
					},
					showFatalDialog: async (detail) => {
						const options = {
							type: 'error' as const,
							title: 'Open DJ engine stopped',
							message: 'Open DJ engine stopped',
							detail,
							buttons: ['Relaunch', 'Quit'],
							defaultId: 0,
							cancelId: 1
						};
						const choice = window === null ? await dialog.showMessageBox(options) : await dialog.showMessageBox(window, options);
						return choice.response === 0 ? 'relaunch' : 'quit';
					},
					quit: (code) => exitApp(code, `engine fatal (code ${code})`)
				};
				supervisor = new EngineSupervisor(
					started,
					{ payload, dataDir, logPath, productName },
					health,
					surface,
					defaultDeps(lockProbe)
				);
			}
		} catch (error) {
			if (supervisor !== null) await supervisor.shutdown();
			failVisibly(error);
		}

		// P15: the window, on the bootstrap page, sized to the screen.
		const size = startingWindowSize(screen.getPrimaryDisplay()?.workAreaSize ?? null);
		window = new BrowserWindow({
			width: size.width,
			height: size.height,
			title: productName,
			backgroundColor: '#000000',
			webPreferences: {
				preload: path.join(__dirname, 'preload.js'),
				contextIsolation: true,
				sandbox: true,
				nodeIntegration: false,
				webSecurity: true,
				spellcheck: false,
				// A DJ app keeps its timers running when another app has focus.
				backgroundThrottling: false
			}
		});
		// The title is the shell's status line (engine restarting, dead), not the page's.
		window.on('page-title-updated', (event) => event.preventDefault());
		window.on('close', (event) => {
			if (quitAllowed) return;
			event.preventDefault();
			appendShellLog('shutdown', 'window-close requested; delegating to webview quit gate');
			requestQuitFromWebview('window-close');
		});
		const contents = window.webContents;
		contents.on('did-start-navigation', (details) => {
			if (details.isMainFrame) appendShellLog('webview', `navigation started: ${details.url}`);
		});
		contents.on('did-navigate', (_event, url) => appendShellLog('webview', `navigation finished: ${url}`));
		const guardNavigation = (event: { preventDefault: () => void }, url: string): void => {
			if (isTrustedPage(url)) return;
			event.preventDefault();
			appendShellLog('WARN', `refused navigation to ${url}`);
			if (isExternalUrlAllowed(url) && new URL(url).protocol === 'https:') void shell.openExternal(url);
		};
		contents.on('will-navigate', (event, url) => guardNavigation(event, url));
		contents.on('will-redirect', (event, url) => guardNavigation(event, url));
		contents.setWindowOpenHandler(({ url }) => {
			if (isExternalUrlAllowed(url) && new URL(url).protocol === 'https:') void shell.openExternal(url);
			return { action: 'deny' };
		});

		// The supervisor starts before the load settles: the bootstrap page can
		// navigate to the engine before loadURL resolves, which rejects it with
		// ERR_ABORTED, and that must not leave the engine unwatched.
		supervisor?.start();
		window.loadURL(`${APP_ORIGIN}/index.html`).catch((error: unknown) => {
			appendShellLog('webview', `bootstrap load settled early: ${String(error)}`);
		});
	}).catch((error: unknown) => failVisibly(error));
}
