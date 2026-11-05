/**
 * The undo stack this window's library writes land on. The server requires
 * X-Undo-Session on every organization write, so a caller never lands on a
 * stack it did not choose (PRD s6.9, E31: undo does not cross a relaunch).
 */
let _session: string | null = null;

export function undoSessionId(): string {
	_session ??= crypto.randomUUID();
	return _session;
}
