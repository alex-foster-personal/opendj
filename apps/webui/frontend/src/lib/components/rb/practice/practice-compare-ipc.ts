/**
 * Issue #9406, ADR-0137: browser IPC for the practice compare page (agent parity).
 * AGENTS.md: UI controls share typed dispatch + serializable query state with agents.
 *
 * `window.musicDjToolsPracticeCompare` runs the same reducer the page uses; it does not
 * mount the performance engine (`performance-ipc.svelte.ts`).
 */
import {
	type CompareCommand,
	type CompareQuery,
	buildCompareReport,
	parseCompareCommand
} from './practice-compare.ts';

interface PracticeCompareOwner {
	dispatch(cmd: CompareCommand): Readonly<CompareQuery>;
	query(): Readonly<CompareQuery>;
	report(): ReturnType<typeof buildCompareReport>;
}

interface PracticeCompareIpc {
	version: 1;
	query(): Readonly<CompareQuery>;
	dispatch(cmd: unknown): Readonly<CompareQuery>;
	set_mode(mode: unknown): Readonly<CompareQuery>;
	set_opacity(pct: unknown): Readonly<CompareQuery>;
	answer(letter: unknown, rating: unknown): Readonly<CompareQuery>;
	goto(index: unknown): Readonly<CompareQuery>;
	report(): ReturnType<typeof buildCompareReport>;
}

declare global {
	interface Window {
		musicDjToolsPracticeCompare?: PracticeCompareIpc;
	}
}

function _letter(v: unknown, name: string): 'A' | 'B' {
	if (v !== 'A' && v !== 'B') {
		throw new TypeError(`${name} expects A or B`);
	}
	return v;
}

function _rating(v: unknown, name: string): number {
	if (typeof v !== 'number' || !Number.isFinite(v)) {
		throw new TypeError(`${name} expects a number`);
	}
	return v;
}

function _index(v: unknown, name: string): number {
	if (typeof v !== 'number' || !Number.isFinite(v)) {
		throw new TypeError(`${name} expects an integer index`);
	}
	if (!Number.isInteger(v)) throw new TypeError(`${name} expects an integer index`);
	return v;
}

export function createPracticeCompareIpc(owner: PracticeCompareOwner): PracticeCompareIpc {
	const dispatch = (cmd: unknown) => {
		const parsed = parseCompareCommand(cmd);
		return owner.dispatch(parsed);
	};
	return Object.freeze({
		version: 1 as const,
		query: () => owner.query(),
		dispatch,
		set_mode(mode: unknown) {
			return dispatch({ type: 'set_mode', mode });
		},
		set_opacity(pct: unknown) {
			return dispatch({ type: 'set_opacity', pct: pct as number });
		},
		answer(letter: unknown, rating: unknown) {
			return dispatch({
				type: 'answer',
				letter: _letter(letter, 'answer'),
				rating: _rating(rating, 'answer')
			});
		},
		goto(index: unknown) {
			return dispatch({ type: 'goto', index: _index(index, 'goto') });
		},
		report: () => owner.report()
	});
}

export function installPracticeCompareIpc(ipc: PracticeCompareIpc): () => void {
	if (typeof window === 'undefined')
		throw new Error('Practice compare IPC requires a browser window');
	if (window.musicDjToolsPracticeCompare !== undefined) {
		throw new Error('Practice compare IPC is already installed');
	}
	window.musicDjToolsPracticeCompare = ipc;
	return () => {
		if (window.musicDjToolsPracticeCompare !== ipc) {
			throw new Error('Practice compare IPC ownership changed before cleanup');
		}
		delete window.musicDjToolsPracticeCompare;
	};
}

export type { PracticeCompareIpc, PracticeCompareOwner };
