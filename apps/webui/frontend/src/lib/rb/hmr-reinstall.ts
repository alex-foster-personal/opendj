/**
 * Dev hot-update safety for module-scope install-once ports.
 *
 * Several modules install a port into ANOTHER module at load time (a runner,
 * a sink), and the target refuses a second install because in production a
 * second one is a wiring bug. A Vite hot update re-runs the installing module
 * while the target keeps its state, so the re-run hits that guard and every
 * importer is left on a broken module graph until a full reload.
 *
 * `import.meta.hot.dispose` is not enough on its own: Vite only calls the
 * disposer of the module a boundary accepts directly, never of a module
 * re-run further down a non-accepting import chain (vite 6 client
 * `fetchUpdate`). `hot.data` survives every re-run of a module, so the
 * previous instance's release is kept there and called first.
 *
 * In production `hot` is undefined: the release is discarded and the guard
 * keeps its once-only meaning.
 */

type HotContext = ImportMeta['hot'];

export function reinstallAcrossHotUpdates(
	hot: HotContext,
	key: string,
	install: () => () => void
): void {
	const previous: unknown = hot?.data[key];
	if (typeof previous === 'function') {
		hot!.data[key] = undefined;
		previous();
	}
	const release = install();
	if (hot) hot.data[key] = release;
}
