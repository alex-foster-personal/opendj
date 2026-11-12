/** Configurable invoke stub for native-shell unit tests (`globalThis.__tauriInvokeMock`). */
export async function invoke(command, args) {
	const fn = globalThis.__tauriInvokeMock;
	if (typeof fn !== 'function') {
		throw new Error(`no Tauri invoke mock for ${command}`);
	}
	return fn(command, args);
}
