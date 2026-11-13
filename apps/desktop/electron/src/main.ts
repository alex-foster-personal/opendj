// Open DJ desktop shell on Electron: entry point.
//
// THIN SHELL WITH A LIFECYCLE (ADR-0048, apps/desktop/README.md). This process
// owns no application logic: no product decisions, no data access, no HTTP
// client for the API. It starts the engine that ships inside the app, keeps it
// alive, opens a Chromium window onto it, and offers the page five native
// features, plus the docked Spotify side player (side-player.ts). Everything a user sees is served by the engine over HTTP.
//
// Port of apps/desktop/src-tauri/src/main.rs. Behaviors are numbered P1-P19 in
// (private path).

import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { pathToFileURL } from 'node:url';

import {
	BrowserWindow,
	Menu,
	MenuItem,
	app,
	dialog,
	ipcMain,
	nativeImage,
	net,
	protocol,
	screen,
	session,
	shell,
	type IpcMainEvent,
	type MenuItemConstructorOptions,
	type IpcMainInvokeEvent
} from 'electron';

import { defaultBootDeps, startEngine } from './boot';
import { installEditContextMenu } from './context-menu';
import { resolveEngineDataDir } from './data-dir';
import { PAYLOAD_DIR } from './engine';
import { makeLockProbe, performancePageUrl } from './launch';
import { isOpenerUrlAllowed } from './opener';
import { payloadPython } from './platform';
import { resolveListCoreAudioOutputs } from './coreaudio-list';
import { outputHealthJson, reconnectSpeakerJson } from './output-health';
import {
	APP_HOST,
	APP_ORIGIN,
	APP_SCHEME,
	appNameFromPackage,
	AUDIO_ENGINE_CHOICE_FILE,
	type PageInit,
	devCdpPortFromEnv,
	diagnosticOutput,
	isAppPage,
	classifyMainFrameNavigation,
	packagedRemoteDebuggingRefusal,
	REMOTE_DEBUGGING_SWITCHES,
	isTrustedPage,
	macAppSupportDir,
	macMenuTemplate,
	permissionAllowed,
	readAudioEngineChoice,
	readBuildStamp,
	shellBuildIdentity,
	startingWindowSize,
	validateDataDirContainedInHome,
	writeAudioEngineChoice
} from './policy';
import { ShellHealthServer } from './shell-health';
import { SidePlayer } from './side-player';
import { sideCollapsedArg, sideOpenArgs } from './side-player-layout';
import {
	SECOND_SCREEN_ACCELERATOR,
	SECOND_SCREEN_MENU_LABEL,
	SECOND_SCREEN_WINDOW_LABEL,
	SecondScreenController,
	WINDOW_LABELS_STATE_FILE,
	parseWindowIpcArgs,
	parseWindowLabelsState,
	serializeWindowLabelsState,
	type SavedGeometry
} from './second-screen';
import { EngineError, appendShellLog, installShellLogging } from './shell-log';
import {
	EngineSupervisor,
	type Supervised,
	type SupervisorSurface,
	defaultDeps,
	shutdownSupervised,
	supervisedOrigin
} from './supervisor';
import { applyUpdate } from './updater';
import {
	handleBeforeQuit,
	handleWillQuit,
	type MainWindowLifecycleDeps,
	type MainWindowLifecycleState,
	wireMainWindowLifecycle
} from './main-window-lifecycle';

/** Test seam, as in the Tauri shell: drive the app against an engine someone else started. */
const ENGINE_ORIGIN_ENV = 'OPENDJ_ENGINE_ORIGIN';
const ENGINE_LOG = path.join('logs', 'engine.log');
/** The bundle identifier the Tauri build uses; keeps the same library folder (P9, rule 2). */
const DEFAULT_IDENTIFIER = 'com.opendj.desktop';

// ----- P1: diagnostic flags, before anything touches the disk ----------------
if (require.main === module) {
	const argv = process.argv.slice(app.isPackaged ? 1 : 2);
	const packagedCdpRefusal = packagedRemoteDebuggingRefusal(
		app.isPackaged,
		process.argv,
		REMOTE_DEBUGGING_SWITCHES.some((name) => app.commandLine.hasSwitch(name))
	);
	const diagnostic = diagnosticOutput(argv, app.getVersion());
	if (packagedCdpRefusal !== null) {
		for (const name of REMOTE_DEBUGGING_SWITCHES) app.commandLine.removeSwitch(name);
		process.stderr.write(`${packagedCdpRefusal}\n`);
		app.exit(1);
	} else if (diagnostic !== null) {
		process.stdout.write(diagnostic);
		app.exit(0);
	} else {
		main();
	}
}

/** The packed package.json, or null when it cannot be read (the defaults then apply). */
function readPackageJson(): unknown {
	try {
		return JSON.parse(fs.readFileSync(path.join(app.getAppPath(), 'package.json'), 'utf8')) as unknown;
	} catch {
		return null;
	}
}

function readIdentifier(): string {
	const pkg = readPackageJson() as { opendj?: { identifier?: unknown } } | null;
	const id = pkg?.opendj?.identifier;
	return typeof id === 'string' && /^[A-Za-z0-9.-]+$/.test(id) ? id : DEFAULT_IDENTIFIER;
}

/**
 * The engine's data dir: where Tauri's `app_data_dir()` puts it, so switching
 * shells keeps the user's library. On macOS that is read from `$HOME`, as Tauri
 * reads it, and refused when it is not inside `$HOME` (#6068); see
 * `macAppSupportDir` and `pinChromiumDirsToHome`. On Windows it is
 * %LOCALAPPDATA%/<identifier> (src/data-dir.ts, never Roaming). Unpackaged
 * builds accept OPENDJ_SHELL_DATA_DIR so tests never touch a real library.
 * Throws EngineError when the folder is refused.
 */
function engineDataDir(identifier: string): string {
	const override = process.env.OPENDJ_SHELL_DATA_DIR;
	if (!app.isPackaged && override !== undefined && override.trim() !== '') return path.resolve(override);
	const dataDir = resolveEngineDataDir(identifier, {
		platform: process.platform,
		env: process.env,
		homedir: os.homedir(),
		appDataDir: app.getPath('appData')
	});
	if (process.platform === 'darwin') {
		try {
			validateDataDirContainedInHome(dataDir, process.env.HOME);
		} catch (error) {
			throw new EngineError('Open DJ refused its data folder.', (error as Error).message);
		}
	}
	return dataDir;
}

/**
 * Chromium's own profile (and the single-instance lock in it) follows `$HOME`
 * too, so a HOME-redirected run neither reads the real profile nor collides
 * with a real running app's lock. For a real user the paths are unchanged.
 * Must run before the single-instance lock and before ready.
 */
function pinChromiumDirsToHome(): void {
	if (process.platform !== 'darwin') return;
	const home = process.env.HOME;
	if (home === undefined || !path.isAbsolute(home)) return; // engineDataDir refuses this case loudly.
	const appData = macAppSupportDir(home);
	app.setPath('appData', appData);
	app.setPath('userData', path.join(appData, app.getName()));
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

/** Log, show the native error box (safe before ready), and exit 1. */
function reportFatal(error: unknown): void {
	const headline = error instanceof EngineError ? error.headline : 'Open DJ cannot start.';
	const detail = error instanceof EngineError ? error.detail : String(error instanceof Error ? error.message : error);
	appendShellLog('ERROR', `${headline}\n\n${detail}`);
	dialog.showErrorBox('Open DJ cannot start', `${headline}\n\n${detail}`);
	app.exit(1);
}

function failVisibly(error: unknown): never {
	reportFatal(error);
	throw error;
}

/** Tauri mark with the dark half painted white, so this shell is not the Tauri app. */
function shellIconPath(): string {
	return path.join(__dirname, '..', 'assets', 'icon.png');
}

function applyShellMark(): void {
	const icon = nativeImage.createFromPath(shellIconPath());
	if (icon.isEmpty()) {
		appendShellLog('WARN', `shell icon missing at ${shellIconPath()}`);
		return;
	}
	if (process.platform === 'darwin' && app.dock !== undefined) app.dock.setIcon(icon);
	appendShellLog('shell', 'dock icon: tauri mark, lower half white');
}

function main(): void {
	// Before pinChromiumDirsToHome and the single-instance lock: the name picks the userData folder both use.
	app.setName(appNameFromPackage(readPackageJson()));
	// P19/D10 / ELECTRON-6877: CDP only for unpackaged builds (CLI refusal above).
	const cdpPort = devCdpPortFromEnv(app.isPackaged, process.env.OPENDJ_REMOTE_DEBUGGING_PORT);
	if (cdpPort !== null) {
		app.commandLine.appendSwitch('remote-debugging-port', cdpPort);
		app.commandLine.appendSwitch('remote-debugging-address', '127.0.0.1');
	}

	protocol.registerSchemesAsPrivileged([
		{ scheme: APP_SCHEME, privileges: { standard: true, supportFetchAPI: true, codeCache: true } }
	]);

	pinChromiumDirsToHome();

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
	let sidePlayer: SidePlayer | null = null;
	let supervisor: EngineSupervisor | null = null;
	const windowLifecycle: MainWindowLifecycleState = {
		quitAllowed: false,
		shuttingDown: null,
		rendererReloadsMs: [],
		lastTrustedUrl: null
	};
	let dataDir: string;
	try {
		dataDir = engineDataDir(identifier);
	} catch (error) {
		// Before any store, log or engine exists (#6068); nothing to clean up.
		reportFatal(error);
		return;
	}
	const engineChoiceFile = path.join(dataDir, AUDIO_ENGINE_CHOICE_FILE);
	const windowLabelsFile = path.join(dataDir, WINDOW_LABELS_STATE_FILE);
	const saveGeometry = (label: string, geometry: SavedGeometry): void => {
		let labels: Record<string, SavedGeometry> = {};
		try {
			labels = parseWindowLabelsState(fs.readFileSync(windowLabelsFile, 'utf8'));
		} catch {
			labels = {};
		}
		labels[label] = geometry;
		try {
			fs.writeFileSync(windowLabelsFile, serializeWindowLabelsState(labels));
		} catch (error) {
			appendShellLog('WARN', `second-screen geometry not saved: ${String(error)}`);
		}
	};
	const secondScreen = new SecondScreenController({
		origin: () => page.engineOrigin,
		mainBounds: () => window?.getBounds() ?? { x: 0, y: 0, width: 1280, height: 800 },
		displays: () => ({
			items: screen.getAllDisplays().map((entry) => ({
				id: entry.id,
				bounds: entry.bounds,
				workArea: entry.workArea
			})),
			primaryId: screen.getPrimaryDisplay().id
		}),
		createWindow: ({ bounds, url, onClosed }) => {
			const extra = new BrowserWindow({
				x: bounds.x,
				y: bounds.y,
				width: bounds.width,
				height: bounds.height,
				title: 'Open DJ - Library (second screen)',
				icon: shellIconPath(),
				backgroundColor: '#000000',
				webPreferences: {
					preload: path.join(__dirname, 'preload.js'),
					contextIsolation: true,
					sandbox: true,
					nodeIntegration: false,
					webSecurity: true,
					spellcheck: false,
					backgroundThrottling: false
				}
			});
			extra.on('page-title-updated', (event) => event.preventDefault());
			// The extra window has no quit gate: closing it must leave the main window and audio running.
			const persist = (): void => {
				if (extra.isDestroyed()) return;
				saveGeometry(SECOND_SCREEN_WINDOW_LABEL, extra.getBounds());
			};
			extra.on('moved', persist);
			extra.on('resized', persist);
			extra.on('close', persist);
			extra.on('closed', () => {
				onClosed();
				publishWindowLabelsToMainWindow();
			});
			const contents = extra.webContents;
			installEditContextMenu(contents);
			const guardNavigation = (event: { preventDefault: () => void }, next: string): void => {
				const action = classifyMainFrameNavigation(next);
				if (action === 'allow') return;
				event.preventDefault();
				appendShellLog('WARN', `refused second-screen navigation to ${next}`);
				if (action === 'open-external') void shell.openExternal(next);
			};
			contents.on('will-navigate', (event, next) => guardNavigation(event, next));
			contents.on('will-redirect', (event, next) => guardNavigation(event, next));
			contents.setWindowOpenHandler(({ url: next }) => {
				if (classifyMainFrameNavigation(next) === 'open-external') void shell.openExternal(next);
				return { action: 'deny' };
			});
			extra.loadURL(url).catch((error: unknown) => {
				appendShellLog('webview', `second-screen load settled early: ${String(error)}`);
			});
			return {
				focus: () => {
					if (!extra.isDestroyed()) {
						extra.show();
						extra.focus();
					}
				},
				close: () => {
					if (!extra.isDestroyed()) extra.close();
				},
				isDestroyed: () => extra.isDestroyed()
			};
		},
		loadGeometry: (label) => {
			try {
				return parseWindowLabelsState(fs.readFileSync(windowLabelsFile, 'utf8'))[label] ?? null;
			} catch {
				return null;
			}
		},
		saveGeometry
	});
	const currentWindowLabels = (): string[] =>
		secondScreen.isOpen() ? [SECOND_SCREEN_WINDOW_LABEL] : [];
	const publishWindowLabelsToMainWindow = (): void => {
		if (window === null || window.webContents.isDestroyed()) return;
		window.webContents.send('opendj:window-labels', currentWindowLabels());
	};
	let savedEngineChoice: PageInit['audioEngineChoice'] = null;
	try {
		savedEngineChoice = readAudioEngineChoice(engineChoiceFile);
	} catch (error) {
		// The page falls back to its default engine; the log says why.
		appendShellLog('WARN', `audio engine choice unreadable (${String(error)}); using the default`);
	}
	const page: PageInit = {
		engineOrigin: null,
		shellBuild: identity,
		perfProbe: process.env.OPENDJ_PERF_PROBE === '1',
		supervisor: null,
		audioEngineChoice: savedEngineChoice
	};

	const shutdownAll = (reason: string): Promise<void> => {
		if (windowLifecycle.shuttingDown === null) {
			windowLifecycle.shuttingDown = (async () => {
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
		return windowLifecycle.shuttingDown;
	};

	const exitApp = (code: number, reason: string): void => {
		windowLifecycle.quitAllowed = true;
		// A shutdown that could not stop the engine's helpers is a failed exit, never a
		// clean one, whatever the reason for quitting was (Sol P1 on #6830).
		let status = code;
		void shutdownAll(reason)
			.catch((error: unknown) => {
				appendShellLog('shutdown', `[ERROR] shell exit: ${(error as Error).message}`);
				if (status === 0) status = 1;
			})
			.finally(() => app.exit(status));
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

	app.on('before-quit', (event) => handleBeforeQuit(event, windowLifecycle, requestQuitFromWebview));
	app.on('will-quit', (event) => handleWillQuit(event, windowLifecycle, () => exitApp(0, 'will-quit')));
	app.on('window-all-closed', () => exitApp(0, 'all windows closed'));

	// ----- native bridge IPC, origin-checked (D4) ---------------------------
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
		if (typeof url !== 'string' || !isOpenerUrlAllowed(url)) throw new Error(`refusing to open ${String(url)}`);
		await shell.openExternal(url);
	});
	ipcMain.handle('opendj:exit', (event, code: unknown) => {
		if (!trusted(event)) return refuse();
		const exitCode = Number.isInteger(code) ? (code as number) : 0;
		exitApp(exitCode, `programmatic-exit(code=${exitCode})`);
	});
	ipcMain.handle('opendj:audio-engine-choice', (event, choice: unknown) => {
		if (!trusted(event)) return refuse();
		// Later page loads this launch read it from page-init too.
		page.audioEngineChoice = writeAudioEngineChoice(engineChoiceFile, choice);
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
					windowLifecycle.quitAllowed = true;
				}
			},
			(progress) => event.sender.send('opendj:update-progress', progress)
		);
	});
	ipcMain.handle('opendj:list-coreaudio-outputs', (event) => {
		if (!trusted(event)) return refuse();
		return resolveListCoreAudioOutputs(payloadDir());
	});

	// ----- the Spotify side player (SPOT-6555b/03), bridge version 2 -------------
	const side = (): SidePlayer => {
		if (window === null) throw new Error('no app window yet');
		sidePlayer ??= new SidePlayer(window);
		return sidePlayer;
	};
	ipcMain.handle('opendj:side-open', async (event, url: unknown, options: unknown) => {
		if (!trusted(event)) return refuse();
		const args = sideOpenArgs(url, options);
		await side().open(args.url, { shrinkMain: args.shrinkMain });
	});
	ipcMain.handle('opendj:side-collapse', (event, collapsed: unknown) => {
		if (!trusted(event)) return refuse();
		// Check the flag before the closed-player short circuit, so a wrong type
		// is refused whether or not a player is open.
		const flag = sideCollapsedArg(collapsed);
		sidePlayer?.setCollapsed(flag);
	});
	ipcMain.handle('opendj:side-close', (event) => {
		if (!trusted(event)) return refuse();
		sidePlayer?.close();
	});
	ipcMain.handle('opendj:side-import-logins', async (event) => {
		if (!trusted(event)) return refuse();
		return side().importBrowserLogins();
	});
	ipcMain.handle('opendj:window', (event, action: unknown, label: unknown) => {
		if (!trusted(event)) return refuse();
		const parsed = parseWindowIpcArgs(action, label);
		secondScreen.control(parsed.action, parsed.label);
		publishWindowLabelsToMainWindow();
	});
	ipcMain.handle('opendj:window-labels', (event) => {
		if (!trusted(event)) return refuse();
		return currentWindowLabels();
	});

	void app.whenReady().then(async () => {
		installSignalHandlers();
		const logPath = path.join(dataDir, ENGINE_LOG);
		try {
			fs.mkdirSync(dataDir, { recursive: true });
			installShellLogging(logPath);
		} catch (error) {
			failVisibly(error);
		}
		appendShellLog('INFO', `electron shell ${app.getVersion()} (electron ${process.versions.electron}) data dir ${dataDir}`);
		applyShellMark();
		if (process.platform === 'darwin') {
			Menu.setApplicationMenu(
				Menu.buildFromTemplate(macMenuTemplate(productName, app.isPackaged) as MenuItemConstructorOptions[])
			);
			const menu = Menu.getApplicationMenu();
			const windowItem = menu?.items.find((item) => {
				const role = (item.role ?? '').toLowerCase();
				return role === 'window' || role === 'windowmenu';
			});
			if (windowItem?.submenu !== undefined) {
				windowItem.submenu.append(new MenuItem({ type: 'separator' }));
				windowItem.submenu.append(
					new MenuItem({
						label: SECOND_SCREEN_MENU_LABEL,
						accelerator: SECOND_SCREEN_ACCELERATOR,
						click: () => {
							try {
								secondScreen.control('open', SECOND_SCREEN_WINDOW_LABEL);
								publishWindowLabelsToMainWindow();
							} catch (error) {
								appendShellLog('WARN', `second-screen menu: ${String(error)}`);
							}
						}
					})
				);
			}
		}

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
		const lockProbe = makeLockProbe(payloadPython(payload));
		const external = process.env[ENGINE_ORIGIN_ENV];
		let health: ShellHealthServer | null = null;
		let bootEngine: Supervised | undefined;
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
					if (choice.response === 0) {
						appendShellLog(
							'INFO',
							`user chose to stop engine pid ${pid} and start a new one`
						);
					}
					return choice.response === 0;
				});
				bootEngine = await startEngine(payload, dataDir, logPath, bootDeps);
				page.engineOrigin = supervisedOrigin(bootEngine);
				health = await ShellHealthServer.start(dataDir, (action) =>
					action === 'reconnect' ? reconnectSpeakerJson(payload) : outputHealthJson(action, payload)
				);
				const surface: SupervisorSurface = {
					setTitle: (title) => window?.setTitle(title),
					navigateToOrigin: (origin) => {
						page.engineOrigin = origin;
						page.supervisor = null;
						void window?.loadURL(performancePageUrl(origin));
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
					bootEngine,
					{ payload, dataDir, logPath, productName },
					health,
					surface,
					defaultDeps(lockProbe)
				);
				bootEngine = undefined;
			}
		} catch (error) {
			if (supervisor !== null) await supervisor.shutdown();
			if (bootEngine !== undefined) await shutdownSupervised(bootEngine, dataDir);
			if (health !== null) await health.close();
			failVisibly(error);
			return;
		}

		try {
			// P15: the window, on the bootstrap page, sized to the screen.
			const size = startingWindowSize(screen.getPrimaryDisplay()?.workAreaSize ?? null);
			window = new BrowserWindow({
				width: size.width,
				height: size.height,
				title: productName,
				icon: shellIconPath(),
				backgroundColor: '#000000',
				...(process.platform === 'darwin'
					? { titleBarStyle: 'hidden', trafficLightPosition: { x: 12, y: 8 } }
					: {}),
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
			const contents = window.webContents;
			contents.on('did-start-navigation', (details) => {
				if (details.isMainFrame) appendShellLog('webview', `navigation started: ${details.url}`);
			});
			contents.on('did-navigate', (_event, url) => {
				appendShellLog('webview', `navigation finished: ${url}`);
				if (isTrustedPage(url)) windowLifecycle.lastTrustedUrl = url;
			});
			installEditContextMenu(contents);
			const guardNavigation = (event: { preventDefault: () => void }, url: string): void => {
				const action = classifyMainFrameNavigation(url);
				if (action === 'allow') return;
				event.preventDefault();
				appendShellLog('WARN', `refused navigation to ${url}`);
				if (action === 'open-external') void shell.openExternal(url);
			};
			contents.on('will-navigate', (event, url) => guardNavigation(event, url));
			contents.on('will-redirect', (event, url) => guardNavigation(event, url));
			const lifecycleDeps: MainWindowLifecycleDeps = {
				appendShellLog,
				nowMs: () => Date.now(),
				loadUrl: (webContents, target) => {
					webContents.loadURL(target).catch((error: unknown) => {
						appendShellLog('webview', `crash reload settled early: ${String(error)}`);
					});
				},
				requestQuitFromWebview,
				shutdownAll,
				reportFatal,
				getUrl: (webContents) => webContents.getURL()
			};
			// Row 72: a dead renderer leaves a blank window over a live engine; reload, then give up.
			wireMainWindowLifecycle(window, contents, windowLifecycle, lifecycleDeps);
			contents.setWindowOpenHandler(({ url }) => {
				if (classifyMainFrameNavigation(url) === 'open-external') void shell.openExternal(url);
				return { action: 'deny' };
			});

			// The supervisor starts before the load settles: the bootstrap page can
			// navigate to the engine before loadURL resolves, which rejects it with
			// ERR_ABORTED, and that must not leave the engine unwatched.
			supervisor?.start();
			window.loadURL(`${APP_ORIGIN}/index.html`).catch((error: unknown) => {
				appendShellLog('webview', `bootstrap load settled early: ${String(error)}`);
			});
		} catch (error) {
			if (supervisor !== null) await supervisor.shutdown();
			failVisibly(error);
		}
	}).catch((error: unknown) => failVisibly(error));
}
