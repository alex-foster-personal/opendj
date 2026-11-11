/**
 * Bottom-left connectivity-dot hover copy. Bright = LIB / BE / FE from the
 * same LibraryHealthDot rows BrowserPanel already paints. Soft = Spotify /
 * Rekordbox / djay (light, non-blocking).
 */

import type { LibraryHealthDot } from './library-health-dots';

export const CONN_DOT_POLICY =
	'Nothing leaves this machine until you submit. Copy and paste tips stay on this device.';

export type SoftDotState = {
	id: string;
	label: string;
	up: boolean;
	detail: string;
};

export type BrightId = 'lib' | 'be' | 'fe';

export type ConnDotTip = {
	id: string;
	name: string;
	kind: 'soft' | 'bright';
	up: boolean;
	status: 'up' | 'down' | 'off' | 'unknown';
	meaning: string;
	/** Present when status is not healthy; copy-friendly agent prompts. */
	askAgent?: string[];
};

const BRIGHT: Record<
	BrightId,
	{ name: string; upMeaning: string; downMeaning: string; ask: string[] }
> = {
	lib: {
		name: 'LIB (library)',
		upMeaning: 'Library health is green: tracks expected on this machine resolve.',
		downMeaning: 'Library health is not green: empty, broken links, or still measuring.',
		ask: [
			'Ask agent: check GET /api/v1/health state_db.tracks and that (private path) exists.',
			'Ask agent: run `just watch-servers-once` and report LIB/BE; confirm --data-dir / worktree is correct.',
			'Ask agent: if you recently re-decrypted, verify master.plain.db import into state.db ran.',
			'Ask agent: compare this worktree ports vs another stack stealing the library path.'
		]
	},
	be: {
		name: 'BE (backend API)',
		upMeaning: 'Backend online: FastAPI answered the liveness probe.',
		downMeaning: 'Backend down, refused, or health timed out.',
		ask: [
			'Ask agent: is anything listening on the API port? `just watch-servers-once` and read PEERS.',
			'Ask agent: tail the worktree API log for the last traceback.',
			'Ask agent: confirm uvicorn was started from this checkout, not a stale worktree.',
			'Ask agent: if health is slow but port open, treat as WARN not DIY restart unless authorized.'
		]
	},
	fe: {
		name: 'FE (frontend)',
		upMeaning: 'Frontend online: this origin answered the dev-server probe (or App UI mirrors BE).',
		downMeaning: 'Frontend origin unreachable from the browser.',
		ask: [
			'Ask agent: is Vite up for this worktree? Check `just watch-servers-once`.',
			'Ask agent: confirm the browser URL host/port matches the running FE (wrong wt = red FE).',
			'Ask agent: look for Vite compile errors in the FE terminal.',
			'Ask agent: if only FE is red, leave BE alone; restart Vite only with parent approval.'
		]
	}
};

const SOFT_ASK: Record<string, string[]> = {
	spotify: [
		'Ask agent: is SPOTIFY_CLIENT_ID set (key present, not the value) and token cache non-empty?',
		'Ask agent: follow `(private path)` without opening a browser mid-set.',
		'Ask agent: confirm TOKEN_CACHE_PATH exists and is not zero bytes; never paste tokens into chat.',
		'Ask agent: soft-off is non-blocking; only chase if Spotify source panel is needed now.'
	],
	rekordbox: [
		'Ask agent: does (private path) exist and have size > 0?',
		'Ask agent: if missing, re-decrypt per docs; then refresh anlz-cache only if instructed.',
		'Ask agent: confirm DATA_DIR points at this repo data/, not another worktree.',
		'Ask agent: soft rekordbox-off does not red LIB/BE; library may still serve state.db.'
	],
	djay: [
		'Ask agent: does the djay working DB path exist on the engine (apps.shared.paths.DJAY_WORKING_DB)?',
		'Ask agent: soft djay-off is fine if you are not using djay history/pairings right now.',
		'Ask agent: check djay_db helpers for path resolution without dumping DB contents.',
		'Ask agent: never upload the djay DB in a bug report; only path presence + size.'
	]
};

const SOFT_MEANING_UP: Record<string, string> = {
	spotify: 'Spotify client id + local auth token cache present (soft; non-blocking).',
	rekordbox: 'Decrypted Rekordbox working DB on disk (soft; non-blocking).',
	djay: 'djay working DB on disk (soft; non-blocking).'
};

export function connStatusFromHealthState(state: LibraryHealthDot['state']): ConnDotTip['status'] {
	if (state === 'complete') return 'up';
	if (state === 'error') return 'down';
	if (state === 'unavailable') return 'unknown';
	return 'off';
}

export function brightIdFromHealthDot(dot: LibraryHealthDot): BrightId | null {
	if (dot.label === 'Library health') return 'lib';
	if (dot.label === 'Backend') return 'be';
	if (dot.label === 'Frontend' || dot.label === 'App UI') return 'fe';
	return null;
}

/** Map the live health dot row into a conn tip without inventing parallel copy. */
export function connTipFromHealthDot(dot: LibraryHealthDot): ConnDotTip | null {
	const id = brightIdFromHealthDot(dot);
	if (id === null) return null;
	const meta = BRIGHT[id];
	const status = connStatusFromHealthState(dot.state);
	const up = status === 'up';
	const meaning = `${meta.upMeaning} Live detail: ${dot.detail}`;
	const tip: ConnDotTip = {
		id,
		name: meta.name,
		kind: 'bright',
		up,
		status,
		meaning
	};
	if (!up) tip.askAgent = meta.ask;
	return tip;
}

export function softDotTip(dot: SoftDotState): ConnDotTip {
	const up = Boolean(dot.up);
	const id = String(dot.id || 'soft');
	const name = String(dot.label || id);
	const meaning = up
		? (SOFT_MEANING_UP[id] ?? `${name} soft dependency looks present.`)
		: `Soft off: ${dot.detail || 'missing or unknown'}. Non-blocking for Performance.`;
	const tip: ConnDotTip = {
		id,
		name: `${name} (soft)`,
		kind: 'soft',
		up,
		status: up ? 'up' : 'off',
		meaning
	};
	if (!up) {
		tip.askAgent = SOFT_ASK[id] ?? [
			`Ask agent: inspect soft dependency "${id}" detail: ${dot.detail || 'unknown'}.`,
			'Ask agent: confirm this is expected; soft dots never block LIB/BE/FE.',
			'Ask agent: do not restart servers solely for a soft-off dot.'
		];
	}
	return tip;
}

export function brightDotTip(id: BrightId, up: boolean): ConnDotTip {
	const meta = BRIGHT[id];
	const tip: ConnDotTip = {
		id,
		name: meta.name,
		kind: 'bright',
		up,
		status: up ? 'up' : 'down',
		meaning: up ? meta.upMeaning : meta.downMeaning
	};
	if (!up) tip.askAgent = meta.ask;
	return tip;
}

export function tipTitle(tip: ConnDotTip): string {
	const status =
		tip.status === 'up'
			? 'OK'
			: tip.status === 'down'
				? 'DOWN'
				: tip.status === 'off'
					? 'OFF'
					: 'UNKNOWN';
	const lines = [`${tip.name}: ${status}`, tip.meaning];
	if (tip.askAgent && tip.askAgent.length > 0) {
		lines.push('Ask an agent:');
		for (const a of tip.askAgent.slice(0, 4)) lines.push(`- ${a}`);
	}
	return lines.join('\n');
}

/** Flatten every visible tip for copy-all (clipboard only, no network). */
export function formatCopyAllTips(tips: readonly ConnDotTip[]): string {
	const blocks: string[] = [CONN_DOT_POLICY, ''];
	for (const tip of tips) {
		blocks.push(tipTitle(tip));
		blocks.push('');
	}
	return blocks.join('\n').trim() + '\n';
}

export function shouldShowAskAgent(tip: ConnDotTip): boolean {
	return tip.status === 'down' || tip.status === 'unknown' || tip.status === 'off';
}

export function anyBrightDown(libUp: boolean, beUp: boolean, feUp: boolean): boolean {
	return !(libUp && beUp && feUp);
}
