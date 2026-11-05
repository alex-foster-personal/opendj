/**
 * Hand a track to rekordbox or djay Pro (HANDOFF-01..03).
 *
 * GET /api/v1/handoff/apps says which apps this machine can open a track in;
 * POST /api/v1/tracks/{stable_id}:open-in?app=... asks the OS to open the
 * track's file in that app. Open DJ writes nothing: what the target app does
 * with the file is that app's own behavior.
 */

import { API_BASE, RbApiError } from '$lib/api';
import { refuseStickRead } from './track-source';

export type HandoffAppId = 'rekordbox' | 'djay';

export interface HandoffApp {
	app: HandoffAppId;
	label: string;
	available: boolean;
	reason: string | null;
}

export const HANDOFF_ROUTE_TITLE = 'POST /api/v1/tracks/{stable_id}:open-in';
export const HANDOFF_BUTTONS: readonly { app: HandoffAppId; label: string }[] = [
	{ app: 'rekordbox', label: 'Open in Rekordbox' },
	{ app: 'djay', label: 'Open in djay' }
];

async function _raise(r: Response): Promise<never> {
	let code = `HTTP_${r.status}`;
	let msg = r.statusText;
	try {
		const body = (await r.json()) as { detail?: { code?: string; message?: string } | string };
		if (typeof body.detail === 'string') msg = body.detail;
		else if (body.detail) {
			code = body.detail.code ?? code;
			msg = body.detail.message ?? msg;
		}
	} catch {
		// A non-JSON error body keeps the status line as its message.
	}
	throw new RbApiError(r.status, code, msg);
}

export async function fetchHandoffApps(): Promise<HandoffApp[]> {
	const r = await fetch(`${API_BASE}/api/v1/handoff/apps`);
	if (!r.ok) await _raise(r);
	const body = (await r.json()) as { apps: HandoffApp[] };
	return body.apps;
}

export async function openTrackInApp(stableId: string, app: HandoffAppId): Promise<void> {
	// A stick row has no library file to hand over.
	refuseStickRead(stableId, 'open-in');
	const r = await fetch(
		`${API_BASE}/api/v1/tracks/${encodeURIComponent(stableId)}:open-in?app=${app}`,
		{ method: 'POST' }
	);
	if (!r.ok) await _raise(r);
}

/** What one hand-off button shows, given what the apps probe returned. */
export interface HandoffButtonState {
	disabled: boolean;
	title: string;
}

export function handoffButtonState(
	app: HandoffAppId,
	probe: HandoffApp[] | 'loading' | { error: string }
): HandoffButtonState {
	if (probe === 'loading') {
		return { disabled: true, title: 'Checking whether this app is installed...' };
	}
	if (!Array.isArray(probe)) {
		return { disabled: true, title: `Could not check installed apps: ${probe.error}` };
	}
	const row = probe.find((entry) => entry.app === app);
	if (!row) return { disabled: true, title: `The engine did not report ${app}` };
	if (!row.available)
		return { disabled: true, title: row.reason ?? `${row.label} is not installed` };
	return { disabled: false, title: `Open this file in ${row.label} (${HANDOFF_ROUTE_TITLE})` };
}
