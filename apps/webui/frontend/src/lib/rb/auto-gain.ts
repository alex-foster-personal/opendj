/**
 * Auto gain (AUTOGAIN-01..06): the per-track offset a deck applies at load so
 * tracks play at one loudness. The server resolves it
 * (`GET /api/v1/auto-gain/tracks/{id}`); this module fetches, checks and
 * formats it. The deck applies `effective_offset_db` at the trim stage, under
 * the TRIM knob, so the knob keeps working on top of it.
 *
 * CLI twin: `opendj track auto-gain <id> [set <dB>|clear]`.
 */
import { API_BASE } from '$lib/api';

/** Mirrors `apps/analysis_loudness/auto_gain.py` (pinned by tests/unit/auto-gain.test.mjs). */
export const AUTO_GAIN_DEFAULT_ENABLED = true;
export const AUTO_GAIN_DEFAULT_TARGET_LUFS = -12;
export const AUTO_GAIN_TARGET_MIN_LUFS = -24;
export const AUTO_GAIN_TARGET_MAX_LUFS = -5;
export const AUTO_GAIN_OFFSET_MIN_DB = -12;
export const AUTO_GAIN_OFFSET_MAX_DB = 12;

export type AutoGainStatus = 'analysed' | 'user' | 'not_analysed' | 'off';

export interface AutoGainResolved {
	stable_id: string;
	status: AutoGainStatus;
	enabled: boolean;
	target_lufs: number;
	integrated_lufs: number | null;
	true_peak_dbtp: number | null;
	analysed_offset_db: number | null;
	user_offset_db: number | null;
	effective_offset_db: number;
	limited_by: 'range' | 'true_peak' | null;
	loudness_reason: string | null;
	offset_min_db: number;
	offset_max_db: number;
	true_peak_ceiling_dbtp: number;
}

export interface AutoGainPrefs {
	enabled: boolean;
	target_lufs: number;
}

/** What one deck's auto gain is doing right now. */
export type DeckAutoGain =
	| { state: 'empty' }
	| { state: 'ready'; value: AutoGainResolved }
	| { state: 'error'; stable_id: string; message: string };

const STATUSES: readonly AutoGainStatus[] = ['analysed', 'user', 'not_analysed', 'off'];

function _num(raw: Record<string, unknown>, key: string, nullable: boolean): number | null {
	const value = raw[key];
	if (value === null && nullable) return null;
	if (typeof value !== 'number' || !Number.isFinite(value)) {
		throw new Error(`auto gain response: ${key} must be a finite number${nullable ? ' or null' : ''}, got ${JSON.stringify(value)}`);
	}
	return value;
}

/** Check a server answer field by field; a malformed one throws, never guesses. */
export function parseAutoGain(raw: unknown): AutoGainResolved {
	if (raw === null || typeof raw !== 'object') throw new Error('auto gain response must be an object');
	const o = raw as Record<string, unknown>;
	if (typeof o.stable_id !== 'string' || o.stable_id.length === 0) throw new Error('auto gain response: stable_id missing');
	if (!STATUSES.includes(o.status as AutoGainStatus)) throw new Error(`auto gain response: unknown status ${JSON.stringify(o.status)}`);
	if (typeof o.enabled !== 'boolean') throw new Error('auto gain response: enabled must be a boolean');
	const limited = o.limited_by;
	if (limited !== null && limited !== 'range' && limited !== 'true_peak') throw new Error(`auto gain response: unknown limited_by ${JSON.stringify(limited)}`);
	const reason = o.loudness_reason;
	if (reason !== null && typeof reason !== 'string') throw new Error('auto gain response: loudness_reason must be a string or null');
	const parsed: AutoGainResolved = {
		stable_id: o.stable_id,
		status: o.status as AutoGainStatus,
		enabled: o.enabled,
		target_lufs: _num(o, 'target_lufs', false) as number,
		integrated_lufs: _num(o, 'integrated_lufs', true),
		true_peak_dbtp: _num(o, 'true_peak_dbtp', true),
		analysed_offset_db: _num(o, 'analysed_offset_db', true),
		user_offset_db: _num(o, 'user_offset_db', true),
		effective_offset_db: _num(o, 'effective_offset_db', false) as number,
		limited_by: limited,
		loudness_reason: reason,
		offset_min_db: _num(o, 'offset_min_db', false) as number,
		offset_max_db: _num(o, 'offset_max_db', false) as number,
		true_peak_ceiling_dbtp: _num(o, 'true_peak_ceiling_dbtp', false) as number
	};
	_checkContract(parsed);
	return parsed;
}

/** The resolver's own rules (apps/analysis_loudness/auto_gain.py): anything
 * outside them is a contract break, refused here so Web Audio never applies
 * a value the Rust engine would reject. */
function _checkContract(v: AutoGainResolved): void {
	const fail = (why: string): never => {
		throw new RangeError(`auto gain response for ${v.stable_id}: ${why}`);
	};
	if (v.target_lufs < AUTO_GAIN_TARGET_MIN_LUFS || v.target_lufs > AUTO_GAIN_TARGET_MAX_LUFS) {
		fail(`target_lufs ${v.target_lufs} is outside ${AUTO_GAIN_TARGET_MIN_LUFS}..${AUTO_GAIN_TARGET_MAX_LUFS}`);
	}
	for (const [key, db] of [['effective_offset_db', v.effective_offset_db], ['analysed_offset_db', v.analysed_offset_db], ['user_offset_db', v.user_offset_db]] as const) {
		if (db !== null && (db < AUTO_GAIN_OFFSET_MIN_DB || db > AUTO_GAIN_OFFSET_MAX_DB)) {
			fail(`${key} ${db} is outside ${AUTO_GAIN_OFFSET_MIN_DB}..${AUTO_GAIN_OFFSET_MAX_DB} dB`);
		}
	}
	if (v.enabled !== (v.status !== 'off')) fail(`status ${v.status} disagrees with enabled=${v.enabled}`);
	const expected =
		v.status === 'user' ? v.user_offset_db : v.status === 'analysed' ? v.analysed_offset_db : 0;
	if (expected === null || v.effective_offset_db !== expected) {
		fail(`status ${v.status} must apply ${String(expected)} dB, got effective_offset_db ${v.effective_offset_db}`);
	}
}

/** dB to a linear gain factor. */
export function linearFromDb(db: number): number {
	if (!Number.isFinite(db)) throw new RangeError(`linearFromDb: ${db} is not finite`);
	return 10 ** (db / 20);
}

/** The dB a deck applies: 0 unless ready. */
export function deckAutoGainDb(deck: DeckAutoGain): number {
	return deck.state === 'ready' ? deck.value.effective_offset_db : 0;
}

/** The linear factor a deck multiplies into its trim: 1 unless ready. */
export function deckAutoGainLinear(deck: DeckAutoGain): number {
	return deck.state === 'ready' ? linearFromDb(deck.value.effective_offset_db) : 1;
}

function _signedDb(db: number): string {
	const rounded = Math.round(db * 10) / 10;
	return `${rounded > 0 ? '+' : rounded < 0 ? '-' : ''}${Math.abs(rounded).toFixed(1)} dB`;
}

/** Short readout under the TRIM knob, and its tooltip. */
export function autoGainReadout(deck: DeckAutoGain): { label: string; title: string; tone: 'normal' | 'user' | 'warn' | 'muted' } {
	if (deck.state === 'empty') return { label: 'AG', title: 'Auto gain: no track loaded', tone: 'muted' };
	if (deck.state === 'error') {
		// The message says what is known: a failed read plays at 0 dB, a gain the
		// Rust engine did not acknowledge leaves its applied gain unknown.
		return { label: 'AG ERR', title: `Auto gain: ${deck.message}`, tone: 'warn' };
	}
	const v = deck.value;
	const measured = v.integrated_lufs === null
		? 'no loudness analysis'
		: `measured ${v.integrated_lufs.toFixed(1)} LUFS, true peak ${v.true_peak_dbtp === null ? 'unknown' : `${v.true_peak_dbtp.toFixed(1)} dBTP`}`;
	switch (v.status) {
		case 'off':
			return { label: 'AG OFF', title: `Auto gain is off (Settings). ${measured}.`, tone: 'muted' };
		case 'not_analysed':
			return {
				label: 'AG N/A',
				title: `Auto gain: not analysed, playing at 0 dB. ${v.loudness_reason ?? 'no loudness analysis'}. Click to set your own offset.`,
				tone: 'warn'
			};
		case 'user':
			return {
				label: `AG ${_signedDb(v.effective_offset_db)}*`,
				title: `Auto gain: your own value ${_signedDb(v.effective_offset_db)} (analysed ${v.analysed_offset_db === null ? 'n/a' : _signedDb(v.analysed_offset_db)}, ${measured}). Click to change or reset.`,
				tone: 'user'
			};
		case 'analysed': {
			const limit = v.limited_by === 'true_peak'
				? ` Boost capped so true peak stays at ${v.true_peak_ceiling_dbtp} dBTP.`
				: v.limited_by === 'range' ? ` Clamped to ${v.offset_min_db}..${v.offset_max_db} dB.` : '';
			return {
				label: `AG ${_signedDb(v.effective_offset_db)}`,
				title: `Auto gain ${_signedDb(v.effective_offset_db)} to ${v.target_lufs} LUFS (${measured}).${limit} Click to set your own value.`,
				tone: 'normal'
			};
		}
		default: {
			const _exhaustive: never = v.status;
			throw new Error(`Unhandled auto gain status: ${_exhaustive}`);
		}
	}
}

async function _json(response: Response, what: string): Promise<unknown> {
	if (!response.ok) throw new Error(`${what} failed: HTTP ${response.status} ${await response.text()}`);
	return response.json();
}

export async function fetchTrackAutoGain(stableId: string): Promise<AutoGainResolved> {
	const response = await fetch(`${API_BASE}/api/v1/auto-gain/tracks/${encodeURIComponent(stableId)}`);
	return parseAutoGain(await _json(response, 'auto gain read'));
}

/** Set the user's own offset (dB), or clear it with `null` to use the analysis again. */
export async function putTrackAutoGainUserOffset(stableId: string, offsetDb: number | null): Promise<AutoGainResolved> {
	const response = await fetch(`${API_BASE}/api/v1/auto-gain/tracks/${encodeURIComponent(stableId)}/user-offset`, {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ offset_db: offsetDb })
	});
	return parseAutoGain(await _json(response, 'auto gain user offset'));
}

function _parsePrefs(raw: unknown): AutoGainPrefs {
	const body = raw as { auto_gain?: unknown };
	const ag = body?.auto_gain as Record<string, unknown> | undefined;
	if (ag === undefined || typeof ag.enabled !== 'boolean' || typeof ag.target_lufs !== 'number') {
		throw new Error(`ui-prefs auto_gain malformed: ${JSON.stringify(ag)}`);
	}
	return { enabled: ag.enabled, target_lufs: ag.target_lufs };
}

export async function fetchAutoGainPrefs(): Promise<AutoGainPrefs> {
	return _parsePrefs(await _json(await fetch(`${API_BASE}/api/v1/ui-prefs`), 'ui-prefs read'));
}

export async function putAutoGainPrefs(patch: Partial<AutoGainPrefs>): Promise<AutoGainPrefs> {
	const response = await fetch(`${API_BASE}/api/v1/ui-prefs`, {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ auto_gain: patch })
	});
	return _parsePrefs(await _json(response, 'ui-prefs auto_gain write'));
}
