/**
 * Issue #9898, LIGHT-10573: browser IPC for the lighting stage replay session (agent parity).
 * Replay animates in the browser; `opendj lighting replay` covers the file-at-time read model.
 */
import type { StageCommand, StageQuery } from './stage-session';
import { parseStageCommand } from './stage-session';

interface LightingStageOwner {
	dispatch(cmd: StageCommand): Readonly<StageQuery>;
	query(): Readonly<StageQuery>;
	signature(): string;
}

interface LightingStageIpc {
	version: 1;
	query(): Readonly<StageQuery>;
	signature(): string;
	dispatch(cmd: unknown): Readonly<StageQuery>;
	load_replay(source: unknown, name?: unknown): Readonly<StageQuery>;
	play(): Readonly<StageQuery>;
	pause(): Readonly<StageQuery>;
	seek(elapsed_s: unknown): Readonly<StageQuery>;
	go_live(): Readonly<StageQuery>;
}

declare global {
	interface Window {
		musicDjToolsLightingStage?: LightingStageIpc;
	}
}

function replayText(source: unknown): string {
	if (typeof source === 'string') return source;
	if (source !== null && (typeof source === 'object' || Array.isArray(source))) {
		return JSON.stringify(source);
	}
	throw new TypeError('load_replay expects a string or JSON-serializable value');
}

export function createLightingStageIpc(owner: LightingStageOwner): LightingStageIpc {
	const query = () => owner.query();
	return Object.freeze({
		version: 1 as const,
		query,
		signature: () => owner.signature(),
		dispatch(cmd: unknown) {
			return owner.dispatch(parseStageCommand(cmd));
		},
		load_replay(source: unknown, name?: unknown) {
			const text = replayText(source);
			if (name !== undefined && typeof name !== 'string') {
				throw new TypeError('load_replay: name must be a string when given');
			}
			const cmd: StageCommand =
				name === undefined ? { type: 'load_replay', text } : { type: 'load_replay', text, name };
			return owner.dispatch(cmd);
		},
		play() {
			return owner.dispatch({ type: 'play' });
		},
		pause() {
			return owner.dispatch({ type: 'pause' });
		},
		seek(elapsed_s: unknown) {
			if (typeof elapsed_s !== 'number' || !Number.isFinite(elapsed_s)) {
				throw new TypeError('seek expects a finite number');
			}
			return owner.dispatch({ type: 'seek', elapsed_s });
		},
		go_live() {
			return owner.dispatch({ type: 'go_live' });
		}
	});
}

export function installLightingStageIpc(ipc: LightingStageIpc): () => void {
	if (typeof window === 'undefined') {
		throw new Error('Lighting stage IPC requires a browser window');
	}
	if (window.musicDjToolsLightingStage !== undefined) {
		throw new Error('Lighting stage IPC is already installed');
	}
	window.musicDjToolsLightingStage = ipc;
	return () => {
		if (window.musicDjToolsLightingStage !== ipc) {
			throw new Error('Lighting stage IPC ownership changed before cleanup');
		}
		delete window.musicDjToolsLightingStage;
	};
}
