/**
 * Bottom-left connectivity-dot hover copy + client bug-report scaffolding.
 * Soft dots = Spotify / Rekordbox / djay (light green). Bright = LIB / BE / FE.
 */

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
		upMeaning: 'state.db has at least one track; library listing can hydrate.',
		downMeaning: 'Library empty or unreachable (0 tracks / missing state.db).',
		ask: [
			'Ask agent: check GET /api/v1/health state_db.tracks and that data/state/state.db exists.',
			'Ask agent: run `just smoke-http` and report LIB/BE; confirm --data-dir / worktree is correct.',
			'Ask agent: if you recently re-decrypted, verify master.plain.db import into state.db ran.',
			'Ask agent: compare this worktree ports (8585/5173) vs another stack stealing the library path.'
		]
	},
	be: {
		name: 'BE (backend API)',
		upMeaning: 'FastAPI on the API base answered /api/v1/health.',
		downMeaning: 'Backend down, refused, or health timed out while unreachable.',
		ask: [
			'Ask agent: is anything listening on :8585? `just watch-servers-once` and read PEERS.',
			'Ask agent: tail /tmp/mdt-8585.log (or the worktree API log) for the last traceback.',
			'Ask agent: confirm uvicorn was started from this checkout, not a stale worktree.',
			'Ask agent: if health is slow but port open, treat as WARN not DIY restart unless authorized.'
		]
	},
	fe: {
		name: 'FE (frontend Vite)',
		upMeaning: 'This origin answered GET / (Vite or production static).',
		downMeaning: 'Frontend origin unreachable from the browser.',
		ask: [
			'Ask agent: is Vite up on :5173 for this worktree? Check `just watch-servers-once`.',
			'Ask agent: confirm the browser URL host/port matches the running FE (wrong wt = red FE).',
			'Ask agent: look for Vite compile errors in the FE terminal; do not mock audio to green-wash.',
			'Ask agent: if only FE is red, leave BE alone; restart Vite only with parent approval.'
		]
	}
};

const SOFT_ASK: Record<string, string[]> = {
	spotify: [
		'Ask agent: is SPOTIFY_CLIENT_ID set (key present, not the value) and token cache non-empty?',
		'Ask agent: follow `.agents/skills/spotify-auth/SKILL.md` without opening a browser mid-set.',
		'Ask agent: confirm TOKEN_CACHE_PATH exists and is not zero bytes; never paste tokens into chat.',
		'Ask agent: soft-off is non-blocking; only chase if Spotify source panel is needed now.'
	],
	rekordbox: [
		'Ask agent: does data/master.plain.db exist and have size > 0?',
		'Ask agent: if missing, re-decrypt per docs; then refresh anlz-cache only if instructed.',
		'Ask agent: confirm DATA_DIR points at this repo data/, not another worktree.',
		'Ask agent: soft rekordbox-off does not red LIB/BE; library may still serve state.db.'
	],
	djay: [
		'Ask agent: does the djay working DB path exist (apps.shared.paths.DJAY_WORKING_DB)?',
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

export function softDotTip(dot: SoftDotState): ConnDotTip {
	const up = Boolean(dot.up);
	const id = String(dot.id || 'soft');
	const name = String(dot.label || id);
	const meaning = up
		? SOFT_MEANING_UP[id] ?? `${name} soft dependency looks present.`
		: `Soft off: ${dot.detail || 'missing or unknown'}. Non-blocking for Performance.`;
	const tip: ConnDotTip = {
		id,
		name: `${name} (soft)`,
		kind: 'soft',
		up,
		status: up ? 'up' : 'off',
		meaning
	};
	if (!up) tip.askAgent = SOFT_ASK[id] ?? [
		`Ask agent: inspect soft dependency "${id}" detail: ${dot.detail || 'unknown'}.`,
		'Ask agent: confirm this is expected; soft dots never block LIB/BE/FE.',
		'Ask agent: do not restart servers solely for a soft-off dot.'
	];
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

/** Native `title` / aria one-liner (newlines for multi-line tooltips where supported). */
export function tipTitle(tip: ConnDotTip): string {
	const status =
		tip.status === 'up' ? 'OK' : tip.status === 'down' ? 'DOWN' : tip.status === 'off' ? 'OFF' : 'UNKNOWN';
	const lines = [`${tip.name}: ${status}`, tip.meaning];
	if (tip.askAgent && tip.askAgent.length > 0) {
		lines.push('Ask an agent:');
		for (const a of tip.askAgent.slice(0, 4)) lines.push(`- ${a}`);
	}
	return lines.join('\n');
}

export function anyBrightDown(libUp: boolean, beUp: boolean, feUp: boolean): boolean {
	return !(libUp && beUp && feUp);
}

export type ClientBugReportInput = {
	libUp: boolean;
	beUp: boolean;
	feUp: boolean;
	soft: SoftDotState[];
	apiBase: string;
	origin: string;
	userAgent: string;
	decksPlayingOrAudible: boolean;
	note?: string;
};

const GH_NEW = 'https://github.com/maintainer/music-dj-tools/issues/new';
const BODY_CAP = 1800;

/** Client-side markdown (no secrets). Never auto-send; caller gates on decks idle. */
export function buildClientBugReportMarkdown(input: ClientBugReportInput): string {
	const ts = new Date().toISOString();
	const softLines = input.soft
		.map((d) => `- ${d.label} (${d.id}): ${d.up ? 'ok' : 'off'} -- ${d.detail}`)
		.join('\n');
	const tips = [
		brightDotTip('lib', input.libUp),
		brightDotTip('be', input.beUp),
		brightDotTip('fe', input.feUp),
		...input.soft.map(softDotTip)
	];
	const ask = tips.flatMap((t) => t.askAgent ?? []).slice(0, 8);
	const lines = [
		'# OpenDJ / music-dj-tools bug report (client first-pass)',
		'',
		`generated_at: ${ts}`,
		`api_base: ${input.apiBase}`,
		`origin: ${input.origin}`,
		`user_agent: ${input.userAgent}`,
		`decks_playing_or_audible: ${input.decksPlayingOrAudible}`,
		'',
		'## Conn dots',
		`- LIB (bright): ${input.libUp ? 'up' : 'down'}`,
		`- BE (bright): ${input.beUp ? 'up' : 'down'}`,
		`- FE (bright): ${input.feUp ? 'up' : 'down'}`,
		softLines,
		'',
		'## Ask-agent prompts (from red/gray dots)',
		...(ask.length > 0 ? ask.map((a) => `- ${a}`) : ['- (all dots healthy at capture time)']),
		'',
		'## Privacy',
		'- Never while playing: stop decks / finish the mix before sharing.',
		'- This file has no tokens, .env values, audio, ANLZ, or library DB.',
		'- For a fuller local bundle: `uv run python scripts/bug_report_bundle.py`',
		'- Skill: `.agents/skills/bug-report-diagnostics/SKILL.md`',
		'',
		'## Screenshot',
		'- First pass: not auto-captured. Attach a UI chrome screenshot manually if useful',
		'  (prefer chrome over waveform content when avoidable).',
		'',
		input.note ? `## Note\n${input.note}\n` : ''
	];
	return lines.filter((l) => l !== undefined).join('\n').trim() + '\n';
}

export function githubBugIssueUrl(title: string, body: string): string {
	const capped =
		body.length > BODY_CAP
			? `${body.slice(0, BODY_CAP)}\n\n…(truncated; paste full local report)\n`
			: body;
	const q = new URLSearchParams({
		title,
		body: capped,
		labels: 'bug'
	});
	return `${GH_NEW}?${q.toString()}`;
}

export function defaultBugIssueTitle(input: ClientBugReportInput): string {
	const bad: string[] = [];
	if (!input.libUp) bad.push('LIB');
	if (!input.beUp) bad.push('BE');
	if (!input.feUp) bad.push('FE');
	for (const d of input.soft) {
		if (!d.up) bad.push(d.id);
	}
	const suffix = bad.length > 0 ? bad.join('+') : 'manual';
	return `bug: Performance conn / UX (${suffix})`;
}
