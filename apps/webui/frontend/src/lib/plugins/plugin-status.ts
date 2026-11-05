/**
 * How a plugin row reads on the store page. Pure, type-only imports, so the
 * unit test loads this file directly under --experimental-strip-types.
 */

import type { components } from '../api-types';

type PluginRow = components['schemas']['PluginRowOut'];
type SideloadPreview = components['schemas']['SideloadPreviewOut'];

/** One sentence for where a plugin stands, this session and next start. */
export function statusLine(row: PluginRow): string {
	if (row.pending === 'enable') return 'Turns on next time Open DJ starts';
	if (row.pending === 'disable')
		return row.installed_version === null
			? 'Removed - unloads next time Open DJ starts'
			: 'Turns off next time Open DJ starts';
	if (row.pending === 'update')
		return `Updates to ${row.installed_version} next time Open DJ starts`;
	switch (row.load_status) {
		case 'loaded':
			return 'Running';
		case 'blocked':
			return `Turned off by the store: ${row.load_detail}`;
		case 'dev-only':
			return 'Unsigned developer build - loads only in developer mode';
		case 'no-consumer':
			return 'Installed - nothing in Open DJ uses this yet';
		case 'no-catalog':
			return 'Off until the store can be checked - refresh the store, then restart Open DJ';
		case null:
		case undefined:
			break;
		default:
			return `Did not load (${row.load_status}): ${row.load_detail}`;
	}
	if (row.installed_version !== null && !row.enabled) return 'Installed, turned off';
	return '';
}

export type PluginAction = 'install' | 'update' | 'enable' | 'disable' | null;

/**
 * The one main button a card shows. None while an uninstall waits for a
 * restart: the removed version is still running, so installing it would be refused.
 */
export function primaryAction(row: PluginRow): PluginAction {
	if (row.installed_version === null) {
		if (row.pending === 'disable') return null;
		return row.available_version === null ? null : 'install';
	}
	if (row.update_available) return 'update';
	return row.enabled ? 'disable' : 'enable';
}

/** Badge text for the trust and channel a plugin comes from. */
export function badges(row: PluginRow): string[] {
	const out: string[] = [];
	out.push(row.trust === 'first-party' ? 'Open DJ' : 'Community');
	if (row.channel === 'labs') out.push('Labs');
	if (row.tier === 2) out.push('Third-party terms');
	if (row.tier === 1) out.push('GPL');
	if (row.source === 'sideload') out.push('Unsigned');
	return out;
}

type Screenshot = components['schemas']['ScreenshotOut'];

/**
 * A listing's screenshots for the current skin (PLUG-6522k): ones made in this
 * theme first, then theme-neutral ones, then the other theme, each group in
 * listing order. The first entry is the card's thumbnail.
 */
export function screenshotsFor(shots: Screenshot[], theme: 'dark' | 'light'): Screenshot[] {
	const rank = (s: Screenshot) => (s.theme === theme ? 0 : s.theme === 'any' ? 1 : 2);
	const seen = new Set<string>();
	return shots
		.map((shot, i) => ({ shot, i }))
		.sort((a, b) => rank(a.shot) - rank(b.shot) || a.i - b.i)
		.map(({ shot }) => shot)
		.filter((shot) => {
			// One image listed twice (say for both themes) shows once; the URL is also the gallery's key.
			if (seen.has(shot.url)) return false;
			seen.add(shot.url);
			return true;
		});
}

/** Readable status for a listing; "stable" is the default and says nothing. */
export function listingStatusBadge(status: string): string | null {
	if (status === 'beta') return 'Beta';
	if (status === 'experiment') return 'Experiment';
	if (status === 'deprecated') return 'Deprecated';
	return null;
}

/** Sideload is offered only for a previewed build, and for one with terms only once they are accepted. */
export function canSideload(preview: SideloadPreview | null, noticeRead: boolean): boolean {
	return preview !== null && (preview.notice === null || noticeRead);
}

/** The labs rows to show: all of them when labs is on, else only installed ones, so a hidden catalog never hides an Uninstall. */
export function labsShown<T extends { installed_version: string | null }>(
	rows: T[],
	optIn: boolean
): T[] {
	return optIn ? rows : rows.filter((row) => row.installed_version !== null);
}

/** The confirm dialog's heading, line and button for an install or update, true to what the backend will do. */
export function installConfirmCopy(
	row: { name: string; enabled: boolean; available_version: string | null },
	kind: 'install' | 'update'
): { title: string; line: string; button: string } {
	if (kind === 'install') {
		return {
			title: `Install ${row.name}?`,
			line: 'It turns on next time Open DJ starts.',
			button: 'Install'
		};
	}
	const to = row.available_version ? ` to ${row.available_version}` : '';
	const line = row.enabled
		? 'The new version takes over next time Open DJ starts.'
		: 'The new version is installed and stays off until you turn it on.';
	return { title: `Update ${row.name}${to}?`, line, button: 'Update' };
}
