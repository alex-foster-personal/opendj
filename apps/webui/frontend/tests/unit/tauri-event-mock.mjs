/** Configurable listen stub (`globalThis.__tauriListenMock` returns unlisten). */
export async function listen(event, handler) {
	const fn = globalThis.__tauriListenMock;
	if (typeof fn !== 'function') {
		throw new Error(`no Tauri listen mock for ${event}`);
	}
	return fn(event, handler);
}
