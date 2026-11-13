/**
 * History delete UI wiring for BrowserPanel (PLAYS-10523).
 *
 * The History panel (HistorySourcePanel.svelte, PLAYS-9745) hosts these targets;
 * agents reach the same capability through the HTTP routes and `opendj history`.
 */
import type { MenuRunners } from '$lib/context-menu/bind';
import { confirmAndDeletePlay, deleteHistoryDay, deleteHistoryPlay } from './history-delete';

export type HistoryPlayRow = { stable_id: string; play_id: string; title: string };
export type HistoryDayRow = { date: string; count: number };

export type HistoryBrowserDeps = {
	ask: (cfg: {
		title: string;
		message: string;
		primaryLabel: string;
		secondaryLabel: string;
	}) => Promise<{ ok: boolean }>;
	refresh: () => void | Promise<void>;
	toast: (msg: string, level: 'info' | 'error') => void;
};

let _deps: HistoryBrowserDeps | null = null;

export function wireHistoryBrowserMenu(deps: HistoryBrowserDeps): void {
	_deps = deps;
}

function requireDeps(): HistoryBrowserDeps {
	if (_deps === null) {
		throw new Error('wireHistoryBrowserMenu was not called');
	}
	return _deps;
}

export async function deleteHistoryPlayUi(row: HistoryPlayRow): Promise<void> {
	const deps = requireDeps();
	await confirmAndDeletePlay(
		{
			...deps,
			remove: async () => {
				await deleteHistoryPlay(row.stable_id, row.play_id);
			}
		},
		{
			title: 'Delete play',
			message: `Delete this play of "${row.title}" from history?`,
			done: 'Play removed from history'
		}
	);
}

export async function deleteHistoryDayUi(day: HistoryDayRow): Promise<void> {
	const deps = requireDeps();
	const ok = await confirmAndDeletePlay(
		{
			...deps,
			remove: async () => {
				await deleteHistoryDay(day.date);
			}
		},
		{
			title: 'Delete history day',
			message: `Delete all ${day.count} plays on ${day.date} from history?`,
			done: `Removed plays for ${day.date}`
		}
	);
	void ok;
}

/** Runners for the History panel's play rows, passed to `bindMenuItems`. */
export function historyMenuRunnersForPlay(row: HistoryPlayRow): MenuRunners {
	return { 'delete-play': () => void deleteHistoryPlayUi(row) };
}

export function historyMenuRunnersForDay(day: HistoryDayRow): MenuRunners {
	return { 'delete-day': () => void deleteHistoryDayUi(day) };
}
