/** Browser parity for the normal practice route's inspection selectors (ADR-0137). */
import {
	parsePadLayerCommand,
	type PadLayerCommand,
	type PadLayerQuery
} from './practice-pad-layers';

export interface PracticeLayersOwner {
	dispatch(cmd: PadLayerCommand): PadLayerQuery;
	query(): PadLayerQuery;
}

export interface PracticeLayersIpc {
	version: 1;
	dispatch(cmd: unknown): PadLayerQuery;
	query(): PadLayerQuery;
}

declare global {
	interface Window {
		musicDjToolsPracticeLayers?: PracticeLayersIpc;
	}
}

export function createPracticeLayersIpc(owner: PracticeLayersOwner): PracticeLayersIpc {
	return Object.freeze({
		version: 1 as const,
		dispatch: (cmd: unknown) => owner.dispatch(parsePadLayerCommand(cmd)),
		query: () => owner.query()
	});
}

export function installPracticeLayersIpc(ipc: PracticeLayersIpc): () => void {
	if (typeof window === 'undefined')
		throw new Error('Practice layers IPC requires a browser window');
	if (window.musicDjToolsPracticeLayers !== undefined)
		throw new Error('Practice layers IPC is already installed');
	window.musicDjToolsPracticeLayers = ipc;
	return () => {
		if (window.musicDjToolsPracticeLayers !== ipc)
			throw new Error('Practice layers IPC ownership changed before cleanup');
		delete window.musicDjToolsPracticeLayers;
	};
}
