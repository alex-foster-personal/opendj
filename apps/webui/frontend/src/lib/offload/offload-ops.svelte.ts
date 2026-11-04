/**
 * Live cloud offload operations for the UI (OFFLOAD, spec e8da14b8).
 *
 * The context menus start operations here; the progress tray and the deck's
 * download bar read them; the browser reloads its rows when one ends. Every
 * number shown is the engine's own (`GET /api/v1/offload/operations/{id}`),
 * polled while the operation runs; nothing is estimated.
 */
import { pushToast } from '../stores.svelte';
import {
	cancelOperation,
	getOperation,
	getTrackOffloadStates,
	isFinished,
	offloadPlaylist,
	offloadTracks,
	operationErrors,
	operationLabel,
	restorePlaylist,
	restoreTracks,
	type OffloadOperation,
	type RestoreMode
} from './api-offload';

export const POLL_MS = 750;
/** Consecutive failed progress reads tolerated (a brief engine blip) before the line fails. */
export const MAX_POLL_FAILURES = 4;

type DeckId = 1 | 2 | 3 | 4;

class OffloadOps {
	/** Operations started from this page, newest last; finished ones linger until dismissed. */
	ops = $state<OffloadOperation[]>([]);
	/** The download a deck is waiting on before it loads (the deck's bar). */
	deckDownloads = $state<Partial<Record<DeckId, { stableId: string; op: OffloadOperation }>>>({});
	#listeners = new Set<() => void>();

	onFinished(listener: () => void): () => void {
		this.#listeners.add(listener);
		return () => this.#listeners.delete(listener);
	}

	#put(op: OffloadOperation): void {
		const index = this.ops.findIndex((o) => o.op_id === op.op_id);
		if (index === -1) this.ops = [...this.ops, op];
		else this.ops = this.ops.map((o, i) => (i === index ? op : o));
	}

	dismiss(opId: string): void {
		this.ops = this.ops.filter((o) => o.op_id !== opId);
	}

	/**
	 * Poll ``op`` until it ends; resolves with the final document. A progress
	 * read that keeps failing ends the line as ``failed`` with the reason (the
	 * engine may still finish the work; the rows reload either way), never a
	 * line stuck at "running" (Sentry and Codex on #8601).
	 */
	async follow(op: OffloadOperation, onUpdate?: (op: OffloadOperation) => void): Promise<OffloadOperation> {
		let current = op;
		let failures = 0;
		this.#put(current);
		while (!isFinished(current)) {
			await new Promise((resolve) => setTimeout(resolve, POLL_MS));
			try {
				current = await getOperation(current.op_id);
				failures = 0;
			} catch (error) {
				failures += 1;
				if (failures < MAX_POLL_FAILURES) continue;
				const reason = error instanceof Error ? error.message : String(error);
				current = { ...current, state: 'failed', error: `progress unavailable: ${reason}` };
			}
			this.#put(current);
			onUpdate?.(current);
		}
		for (const listener of this.#listeners) listener();
		return current;
	}

	async #run(start: () => Promise<OffloadOperation>, quiet = false): Promise<OffloadOperation | null> {
		let op: OffloadOperation;
		try {
			op = await start();
		} catch (error) {
			pushToast(`cloud offload refused: ${error instanceof Error ? error.message : String(error)}`, 'error', undefined, error);
			return null;
		}
		const final = await this.follow(op);
		const errors = operationErrors(final);
		if (final.error !== null) pushToast(`${operationLabel(final)}: ${final.error}`, 'error');
		else if (errors.length > 0) pushToast(`${operationLabel(final)}. ${errors.join('; ')}`, 'warn');
		else if (!quiet) pushToast(operationLabel(final), final.state === 'done' ? 'info' : 'warn');
		return final;
	}

	offloadTracks(stableIds: string[]): Promise<OffloadOperation | null> {
		return this.#run(() => offloadTracks(stableIds));
	}

	offloadPlaylist(playlistId: string): Promise<OffloadOperation | null> {
		return this.#run(() => offloadPlaylist(playlistId));
	}

	restoreTracks(stableIds: string[], mode: RestoreMode): Promise<OffloadOperation | null> {
		return this.#run(() => restoreTracks(stableIds, mode));
	}

	restorePlaylist(playlistId: string, mode: RestoreMode): Promise<OffloadOperation | null> {
		return this.#run(() => restorePlaylist(playlistId, mode));
	}

	async cancel(opId: string): Promise<void> {
		try {
			this.#put(await cancelOperation(opId));
		} catch (error) {
			pushToast(`cancel failed: ${error instanceof Error ? error.message : String(error)}`, 'error', undefined, error);
		}
	}

	/**
	 * Load-on-demand: bring an offloaded track back for 24 h ("Download for
	 * 24h"), showing the bar on ``deck``. True when the file is back.
	 */
	async restoreForDeck(stableId: string, deck: DeckId): Promise<boolean> {
		let op: OffloadOperation;
		try {
			op = await restoreTracks([stableId], 'temporary');
		} catch (error) {
			pushToast(`cannot load: the cloud download did not start (${error instanceof Error ? error.message : String(error)})`, 'error', undefined, error);
			return false;
		}
		this.deckDownloads = { ...this.deckDownloads, [deck]: { stableId, op } };
		try {
			const final = await this.follow(op, (update) => {
				this.deckDownloads = { ...this.deckDownloads, [deck]: { stableId, op: update } };
			});
			const result = final.results[stableId];
			if (final.state !== 'done' || result === undefined || result.error !== null) {
				pushToast(`cannot load: cloud download ${final.state} (${result?.message ?? result?.error ?? final.error ?? 'no result'})`, 'error');
				return false;
			}
			return true;
		} finally {
			// Only this download's own entry: a later load on the same deck keeps its bar (Sentry on #8601).
			if (this.deckDownloads[deck]?.op.op_id === op.op_id) {
				const { [deck]: _done, ...rest } = this.deckDownloads;
				this.deckDownloads = rest;
			}
		}
	}
}

export const offloadOps = new OffloadOps();

/**
 * THE deck-load hook (Codex P1 on #8601): every `load` command, from the
 * browser, an agent (`opendj load`, MCP), MIDI or AutoPlay, passes through
 * `dispatchPerformanceCommand`, which awaits this first. An offloaded track
 * is downloaded for 24 h with the bar on that deck; a local one costs one
 * loopback read of the ledger. Throws when the download fails, so the load
 * fails with the reason instead of a missing-file error.
 *
 * An UNREADABLE offload state never blocks a load: offload must not become a
 * new way for a deck load to fail mid-gig. The load goes ahead, and a track
 * that really is offloaded still fails loudly with the audio route's 409
 * `TRACK_OFFLOADED`, so nothing is hidden.
 */
export async function ensureLocalBeforeLoad(deck: DeckId, stableId: string): Promise<void> {
	let state: string | undefined;
	try {
		state = (await getTrackOffloadStates([stableId]))[stableId]?.state;
	} catch (error) {
		console.warn(`cloud offload: state of ${stableId} unreadable, loading without a restore`, error);
		return;
	}
	if (state !== 'offloaded' && state !== 'restoring') return;
	if (!(await offloadOps.restoreForDeck(stableId, deck))) {
		throw new Error(`TRACK_OFFLOADED: ${stableId} could not be downloaded from the cloud`);
	}
}

/** One deck's pending cloud download, as agents read it from the performance state. */
export interface DeckCloudDownload {
	stable_id: string;
	op_id: string;
	state: OffloadOperation['state'];
	bytes_done: number;
	bytes_total: number;
}

/**
 * The deck bars as plain data for `queryPerformanceState().cloud_downloads`
 * (Codex P1 on #8601): an agent that loaded an offloaded track sees the
 * download, and its `op_id` to follow or cancel it, exactly as the bar shows it.
 */
export function deckCloudDownloads(): Record<DeckId, DeckCloudDownload | null> {
	const out = { 1: null, 2: null, 3: null, 4: null } as Record<DeckId, DeckCloudDownload | null>;
	for (const deck of [1, 2, 3, 4] as const) {
		const entry = offloadOps.deckDownloads[deck];
		if (entry === undefined) continue;
		out[deck] = {
			stable_id: entry.stableId,
			op_id: entry.op.op_id,
			state: entry.op.state,
			bytes_done: entry.op.bytes_done,
			bytes_total: entry.op.bytes_total
		};
	}
	return out;
}
