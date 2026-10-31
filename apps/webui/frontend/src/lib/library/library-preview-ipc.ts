/**
 * LIBWS-04: browser IPC for the Crate Desk preview (agent parity, AGENTS.md:
 * a UI-only interaction is incomplete without IPC, CLI or HTTP parity).
 *
 * `window.musicDjToolsLibraryPreview` drives the same player the preview bar
 * does, so an agent's play, pause, seek and stop take the bar's path and read
 * back the bar's state. The preview plays in the browser's own AudioContext,
 * so the browser is the only place these controls can run.
 */
import type { LibraryPreviewPlayer, LibraryPreviewSnapshot } from './library-preview-player';

interface LibraryPreviewBrowserIpc {
	version: 1;
	/** The preview's state: track, status, position and duration (ms), error. */
	query(): Readonly<LibraryPreviewSnapshot>;
	/** Play `stable_id` from `ratio` (0..1), or resume it where it paused when omitted. */
	play(stable_id: unknown, ratio?: unknown): Promise<Readonly<LibraryPreviewSnapshot>>;
	pause(): Readonly<LibraryPreviewSnapshot>;
	/** Move the held track to `ratio` (0..1), as a click on its overview strip does. */
	seek(ratio: unknown): Promise<Readonly<LibraryPreviewSnapshot>>;
	/** Stop and release the preview's context and decoded audio. */
	stop(): Readonly<LibraryPreviewSnapshot>;
}

interface LibraryPreviewIpcDeps {
	player: Pick<LibraryPreviewPlayer, 'play' | 'pause' | 'stop' | 'snapshot'>;
	/**
	 * Why this track cannot play (missing file, streaming), or null. Null for a
	 * track the desk holds no row for: the player's fetch then reports it.
	 */
	refusal(stable_id: string): string | null;
}

declare global {
	interface Window {
		musicDjToolsLibraryPreview?: LibraryPreviewBrowserIpc;
	}
}

function _ratio(value: unknown, name: string): number {
	if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > 1) {
		throw new TypeError(`${name} expects a ratio between 0 and 1`);
	}
	return value;
}

export function createLibraryPreviewIpc(deps: LibraryPreviewIpcDeps): LibraryPreviewBrowserIpc {
	const query = () => Object.freeze({ ...deps.player.snapshot() });
	return Object.freeze({
		version: 1 as const,
		query,
		async play(stable_id: unknown, ratio?: unknown) {
			if (typeof stable_id !== 'string' || stable_id === '') {
				throw new TypeError('play expects a non-empty stable_id');
			}
			const from = ratio === undefined ? undefined : _ratio(ratio, 'play');
			const refusal = deps.refusal(stable_id);
			if (refusal !== null) throw new Error(`preview: ${refusal}`);
			await deps.player.play(stable_id, from);
			return query();
		},
		pause() {
			deps.player.pause();
			return query();
		},
		async seek(ratio: unknown) {
			const to = _ratio(ratio, 'seek');
			const held = deps.player.snapshot().stable_id;
			if (held === null) throw new Error('seek: no preview is loaded');
			await deps.player.play(held, to);
			return query();
		},
		stop() {
			deps.player.stop();
			return query();
		}
	});
}

export function installLibraryPreviewIpc(ipc: LibraryPreviewBrowserIpc): () => void {
	if (typeof window === 'undefined')
		throw new Error('Library preview IPC requires a browser window');
	if (window.musicDjToolsLibraryPreview !== undefined) {
		throw new Error('Library preview IPC is already installed');
	}
	window.musicDjToolsLibraryPreview = ipc;
	return () => {
		if (window.musicDjToolsLibraryPreview !== ipc) {
			throw new Error('Library preview IPC ownership changed before cleanup');
		}
		delete window.musicDjToolsLibraryPreview;
	};
}
