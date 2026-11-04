/**
 * Thin reader for GET /api/v1/performance/telemetry/processes (PERFMODE-05).
 *
 * Maps the privacy-reduced probe snapshot for the open PerfMeters monitor.
 * Poll only while the monitor is open - not on every compact-bar session.
 */

import { API_BASE } from '$lib/api/base';
import type { ProcessFamilyMember, ProcessFamilySnapshot } from './perf-meter-model';

const PROCESSES_PATH = '/api/v1/performance/telemetry/processes';

interface ProcessesResponse {
	available?: unknown;
	kernel_memory_pressure_level?: unknown;
	by_role_mb?: unknown;
	churn_score?: unknown;
	compressor_rate?: unknown;
	compressed_mb?: unknown;
	members?: unknown;
	stale?: unknown;
	age_seconds?: unknown;
}

function _kernelLevelOrNull(value: unknown): number | null {
	return value === 1 || value === 2 || value === 4 ? value : null;
}

function _finiteOrNull(value: unknown): number | null {
	return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/** Probe role slugs from JSONL fallback; never shown raw in PerfMeters (DEVLOOP-11). */
const PROBE_ROLE_TO_OPENDJ_LABEL: Record<string, string> = {
	'python-engine': 'opendj-engine',
	'desktop-shell': 'opendj-desktop',
	'webkit-webcontent': 'opendj-webcontent',
	'webkit-gpu': 'opendj-gpu',
	'webkit-networking': 'opendj-networking',
	'webkit-other': 'opendj-webkit'
};

function _labelFromProbeRole(role: string): string {
	if (role.startsWith('opendj-')) return role;
	return PROBE_ROLE_TO_OPENDJ_LABEL[role] ?? 'unnamed';
}

/** Members from the legacy `by_role_mb` map, already checked by `_rolesProblem`. */
function _membersFromRoles(byRole: Record<string, number>): ProcessFamilyMember[] {
	return Object.entries(byRole).map(([role, mb]) => ({
		label: _labelFromProbeRole(role),
		mb: Math.round(mb),
		role,
		fromProbe: true
	}));
}

function _liveMbFromMember(member: Record<string, unknown>): number | null {
	const rss = _finiteOrNull(member.rss_mb);
	return rss === null ? null : Math.round(rss);
}

/**
 * The MB the meter totals: the probe's physical footprint while the probe field
 * is present (Sol on #6785: it replaces the page estimate), else the live walk's
 * RSS. The route drops the probe field when it expires, so RSS takes over then.
 */
function _mbFromMember(member: Record<string, unknown>): number | null {
	const footprint = _finiteOrNull(member.physical_footprint_mb);
	if (footprint !== null) return Math.round(footprint);
	return _liveMbFromMember(member);
}

/** Members from the `members` list, every entry already checked by `_memberProblem`. */
function _membersFromLive(rawMembers: Record<string, unknown>[]): ProcessFamilyMember[] {
	return rawMembers.map((record) => ({
		label: record.name as string,
		mb: _mbFromMember(record),
		liveMb: _liveMbFromMember(record),
		role: typeof record.role === 'string' && record.role.length > 0 ? record.role : null,
		fromProbe: record.source === 'probe_log',
		probeFields: Array.isArray(record.probe_fields) ? (record.probe_fields as string[]) : []
	}));
}

function _isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function _isFinite(value: unknown): boolean {
	return _finiteOrNull(value) !== null;
}

/**
 * A finite measurement that is not negative. The meter sums MB and reads the
 * pressure values as readings, so a negative one would subtract from the
 * whole-app total or read as a lower load (Codex on #6785). Zero is a reading.
 */
function _isNonNegative(value: unknown): boolean {
	const finite = _finiteOrNull(value);
	return finite !== null && finite >= 0;
}

/** `key` is absent from `record`, or present with a value `valid` accepts. */
function _absentOr(
	record: Record<string, unknown>,
	key: string,
	valid: (value: unknown) => boolean
): boolean {
	return !(key in record) || valid(record[key]);
}

const MEMBER_SOURCES = new Set(['live', 'probe_log']);

function _stringList(value: unknown): boolean {
	return Array.isArray(value) && value.every((field) => typeof field === 'string');
}

/**
 * Why member `index` breaks the route's member contract, or null when it
 * keeps it: an object with a non-empty `name`, and, when present, a string
 * `role`, non-negative `rss_mb` and `physical_footprint_mb`, a known `source` and a
 * list of strings in `probe_fields`. One bad entry makes the WHOLE body
 * malformed (Codex on #6785): skipping it would replace the last good
 * snapshot with a partial one, and the meter would under-report.
 */
function _memberProblem(raw: unknown, index: number): string | null {
	const where = `member ${index}`;
	if (!_isRecord(raw)) return `${where} is not an object`;
	if (typeof raw.name !== 'string' || raw.name.length === 0) return `${where} has no name`;
	if (!_absentOr(raw, 'role', (v) => typeof v === 'string')) {
		return `${where}: \`role\` is not a string`;
	}
	for (const key of ['rss_mb', 'physical_footprint_mb']) {
		if (!_absentOr(raw, key, _isNonNegative)) {
			return `${where}: \`${key}\` is not a non-negative number`;
		}
	}
	if (!_absentOr(raw, 'source', (v) => typeof v === 'string' && MEMBER_SOURCES.has(v))) {
		return `${where}: \`source\` is not a known source`;
	}
	if (!_absentOr(raw, 'probe_fields', _stringList)) {
		return `${where}: \`probe_fields\` is not a list of strings`;
	}
	return null;
}

/** Why the legacy `by_role_mb` map is unusable, or null: non-empty role keys, non-negative MB values. */
function _rolesProblem(byRole: Record<string, unknown>): string | null {
	for (const [role, mb] of Object.entries(byRole)) {
		if (role.length === 0) return '`by_role_mb` has an empty role';
		if (!_isNonNegative(mb)) return `\`by_role_mb\`.${role} is not a non-negative number`;
	}
	return null;
}

/** The kernel level: absent, or a finite number (its own mapping rejects anything but 1, 2, 4). */
const KERNEL_FIELD = 'kernel_memory_pressure_level';
/** The measured pressure fields the snapshot reads: absent, or non-negative. */
const PRESSURE_MEASUREMENTS = ['churn_score', 'compressor_rate', 'compressed_mb'];

/** Why an available body breaks the contract, or null when every field the snapshot reads is valid. */
function _availableProblem(body: Record<string, unknown>): string | null {
	if (!_absentOr(body, KERNEL_FIELD, _isFinite)) return `\`${KERNEL_FIELD}\` is not a finite number`;
	for (const key of PRESSURE_MEASUREMENTS) {
		if (!_absentOr(body, key, _isNonNegative)) {
			return `\`${key}\` is not a non-negative number`;
		}
	}
	if (Array.isArray(body.members)) {
		// The route answers available only when its live walk found a member; an
		// empty list would replace the last valid totals with a measured zero.
		if (body.members.length === 0) return 'an available body has no member';
		for (const [index, member] of body.members.entries()) {
			const problem = _memberProblem(member, index);
			if (problem !== null) return problem;
		}
		return null;
	}
	if ('members' in body || !_isRecord(body.by_role_mb)) {
		return 'an available body has no member list';
	}
	if (Object.keys(body.by_role_mb).length === 0) return 'an available body has no role';
	return _rolesProblem(body.by_role_mb);
}

/** Absent, or a boolean: the only shapes the route gives `stale`. */
function _staleFieldValid(body: Record<string, unknown>): boolean {
	return !('stale' in body) || typeof body.stale === 'boolean';
}

/** Absent, null, or a finite number: the only shapes the route gives `age_seconds`. */
function _ageFieldValid(body: Record<string, unknown>): boolean {
	return !('age_seconds' in body) || body.age_seconds === null || _finiteOrNull(body.age_seconds) !== null;
}

/** One /processes body, checked against the route's discriminated contract. */
export type ProcessesAnswer =
	| { kind: 'available'; snapshot: ProcessFamilySnapshot }
	| { kind: 'unavailable'; stale: { ageSeconds: number | null } | null }
	| { kind: 'malformed'; reason: string };

/**
 * Classify one response body. Only an explicit `available: true` with a
 * member list (or the legacy `by_role_mb` map), or an explicit
 * `available: false`, is an answer; and in either, `stale` and `age_seconds`
 * must have the route's types. An available body must also hold ONLY valid
 * members (or role entries) and pressure fields: one bad entry is never
 * skipped. Anything else (`{}`, `available: "true"`, a non-object, a string
 * age, a `null` member) is a malformed body: it says nothing about the
 * processes, so the caller treats it as a failed request, never as the
 * engine saying "unavailable".
 */
export function parseProcessesResponse(body: unknown): ProcessesAnswer {
	if (!_isRecord(body)) return { kind: 'malformed', reason: 'the body is not a JSON object' };
	if (body.available !== true && body.available !== false) {
		return { kind: 'malformed', reason: '`available` is not a boolean' };
	}
	if (!_staleFieldValid(body)) return { kind: 'malformed', reason: '`stale` is not a boolean' };
	if (!_ageFieldValid(body)) {
		return { kind: 'malformed', reason: '`age_seconds` is not a finite number or null' };
	}
	if (body.available === false) return { kind: 'unavailable', stale: probeStaleFrom(body) };
	const problem = _availableProblem(body);
	if (problem !== null) return { kind: 'malformed', reason: problem };
	return { kind: 'available', snapshot: _snapshotFrom(body) };
}

/**
 * The engine's stale-probe diagnostic, from ANY response, available or not.
 *
 * With no live walk and only a stale probe sample the engine answers
 * `available: false` (there is no current member to show) but still says the
 * sample was stale; that must reach the hover, not vanish with the snapshot.
 */
export function probeStaleFrom(body: ProcessesResponse): { ageSeconds: number | null } | null {
	return body.stale === true ? { ageSeconds: _finiteOrNull(body.age_seconds) } : null;
}

/**
 * Turn one engine response into a snapshot, or null when it is not a valid
 * available body (unavailable, or malformed: see `parseProcessesResponse`,
 * which tells those two apart).
 */
export function processFamilyFrom(body: unknown): ProcessFamilySnapshot | null {
	const answer = parseProcessesResponse(body);
	return answer.kind === 'available' ? answer.snapshot : null;
}

/** The snapshot from an available body that `_availableProblem` accepted. */
function _snapshotFrom(body: ProcessesResponse): ProcessFamilySnapshot {
	return {
		kernelLevel: _kernelLevelOrNull(body.kernel_memory_pressure_level),
		churnScore: _finiteOrNull(body.churn_score),
		compressorRate:
			_finiteOrNull(body.compressor_rate) ?? _finiteOrNull(body.compressed_mb),
		members: Array.isArray(body.members)
			? _membersFromLive(body.members as Record<string, unknown>[])
			: _membersFromRoles(body.by_role_mb as Record<string, number>),
		probeStale: body.stale === true,
		probeAgeSeconds: _finiteOrNull(body.age_seconds)
	};
}

/**
 * How long a native-probe sample counts as current, from its own timestamp;
 * the engine's FRESH_PROCESS_SAMPLE_SECONDS (performance_telemetry.py).
 */
export const PROBE_SAMPLE_FRESH_SECONDS = 45;

function _monoNowMs(): number {
	return performance.now();
}

// Every age and interval in this module is measured on the monotonic clock
// (performance.now), never as a Date.now() delta: a wall clock moved back
// would otherwise keep an old walk "fresh" and hold off the next poll until
// wall time caught up.
let _cached: ProcessFamilySnapshot | null = null;
/**
 * Monotonic start of the request that produced `_cached`; null when none.
 * The WALK's expiry counts from here: the engine walked at some point after
 * it, so this never understates how old the walk is.
 */
let _cachedAtMonoMs: number | null = null;
/**
 * Monotonic time the response that produced `_cached` was received; null when
 * none. The PROBE's age counts on from here, not from the request start: the
 * engine computes `age_seconds` after reading the record and walking the
 * tree, so that work is already inside the age it returned, and counting it
 * again from the request start would retire a sample early (Sol, #6785).
 * What this leaves out is only the engine's work after computing the age
 * (merging members, serializing) and the transfer.
 */
let _probeAgeBaseMonoMs: number | null = null;
let _unavailableStale: { ageSeconds: number | null } | null = null;
let _unavailableAtMonoMs: number | null = null;

/**
 * The stale-probe diagnostic of the last UNAVAILABLE response, while it is no
 * older than `maxAgeMs`; null after an available response or once it ages out.
 */
export function readUnavailableProbeStale(
	maxAgeMs: number,
	monoNowMs: number = _monoNowMs()
): { ageSeconds: number | null } | null {
	if (_unavailableStale === null || _unavailableAtMonoMs === null) return null;
	if (monoNowMs - _unavailableAtMonoMs > maxAgeMs) return null;
	return _unavailableStale;
}

/** Last fetched snapshot, or null if never polled or unavailable. */
export function readProcessFamilySnapshot(): ProcessFamilySnapshot | null {
	return _cached;
}

/**
 * The last snapshot while its fetch is no older than `maxAgeMs`, else null.
 * A failed request keeps the cache (see `fetchProcessFamilySnapshot`), so a
 * reader that shows the processes as CURRENT (the memory figure, the pinned
 * breakdown, the chart, the sampling cadence) reads through this, never
 * `readProcessFamilySnapshot`, and one expiry rule governs them all.
 */
export function readFreshProcessFamilySnapshot(
	maxAgeMs: number,
	monoNowMs: number = _monoNowMs()
): ProcessFamilySnapshot | null {
	if (_cached === null || _cachedAtMonoMs === null || _probeAgeBaseMonoMs === null) return null;
	if (monoNowMs - _cachedAtMonoMs > maxAgeMs) return null;
	return _withinProbeLifetime(_cached, (monoNowMs - _probeAgeBaseMonoMs) / 1000);
}

/**
 * The snapshot with its probe-derived parts dropped once the probe sample
 * they came from is older than PROBE_SAMPLE_FRESH_SECONDS: the age the
 * engine returned plus the time since the response arrived, on a monotonic
 * clock. The snapshot's own expiry is counted from the request start, so without this a sample fetched at 44 s
 * would be shown for up to 30 s more. The live members stay (the engine
 * measured them just now); probe-only members go, and a live member gives
 * back the fields it took from the probe. The result then says the sample
 * is stale, as the engine would.
 */
function _withinProbeLifetime(
	snapshot: ProcessFamilySnapshot,
	elapsedSeconds: number
): ProcessFamilySnapshot {
	if (snapshot.probeStale || snapshot.probeAgeSeconds == null) return snapshot;
	const ageSeconds = snapshot.probeAgeSeconds + Math.max(0, elapsedSeconds);
	if (ageSeconds <= PROBE_SAMPLE_FRESH_SECONDS) return snapshot;
	const members = snapshot.members
		.filter((m) => !m.fromProbe)
		.map((m) => {
			if (!m.probeFields || m.probeFields.length === 0) return m;
			const live: ProcessFamilyMember = { ...m, probeFields: [] };
			if (m.probeFields.includes('role')) live.role = null;
			// The MB too, when it came from the probe's footprint: what is left
			// is the walk's own reading, or null (unreadable) without one.
			if (m.probeFields.includes('physical_footprint_mb')) live.mb = m.liveMb ?? null;
			return live;
		});
	return { ...snapshot, members, probeStale: true, probeAgeSeconds: ageSeconds };
}

/** Monotonic start of the latest attempt; null before the first. */
let _attemptAtMonoMs: number | null = null;
/** Sequence number of the latest attempt started. */
let _attemptSeq = 0;
/** Sequence number of the newest attempt whose answer was applied. */
let _appliedSeq = 0;

/**
 * Fetch the process-family snapshot. Never throws.
 * Returns null when the engine has no sample.
 *
 * Every call counts as an attempt (see `processFamilyRefreshDue`), whatever it
 * returns. An UNAVAILABLE answer is news: the engine has nothing current, so
 * the last snapshot is dropped rather than shown as current, and the
 * stale-probe diagnostic is kept for the hover. A failed request (network
 * error, non-2xx, unreadable body, or a body that breaks the route's contract,
 * such as `{}` or `available: "true"`) says nothing about the processes, so
 * the last snapshot is kept and ages out on its own clock. Only an explicit,
 * valid `available: false` clears it.
 *
 * Fetches can overlap (a pin forcing a refresh every interval while an
 * earlier one is still in flight, or one slow answer). An answer is dropped
 * only when an answer from a NEWER attempt was already applied, so a late,
 * older answer can neither clear a newer snapshot nor restore totals the
 * engine has since withdrawn. Every other answer applies as soon as it
 * arrives, whatever is still pending: under an engine slower than the poll
 * interval each answer lands while the next request is in flight, and must
 * still be applied, or the cache would starve.
 */
export async function fetchProcessFamilySnapshot(): Promise<ProcessFamilySnapshot | null> {
	const seq = ++_attemptSeq;
	// Taken before the request: the engine reads the probe's age while
	// answering, so counting from here never understates the time since.
	const startedMonoMs = _monoNowMs();
	_attemptAtMonoMs = startedMonoMs;
	try {
		const response = await fetch(`${API_BASE}${PROCESSES_PATH}`, {
			headers: { accept: 'application/json' }
		});
		// Taken as the answer arrives: the base the returned probe age counts on from.
		const receivedMonoMs = _monoNowMs();
		if (!response.ok) return _cached;
		const answer = parseProcessesResponse(await response.json());
		// Malformed is a failed request: it neither applies nor outranks an older answer.
		if (answer.kind === 'malformed') return _cached;
		if (seq < _appliedSeq) return _cached;
		_appliedSeq = seq;
		if (answer.kind === 'available') {
			_cached = answer.snapshot;
			_cachedAtMonoMs = startedMonoMs;
			_probeAgeBaseMonoMs = receivedMonoMs;
			_unavailableStale = null;
			return answer.snapshot;
		}
		_cached = null;
		_cachedAtMonoMs = null;
		_probeAgeBaseMonoMs = null;
		_unavailableStale = answer.stale;
		_unavailableAtMonoMs = startedMonoMs;
		return null;
	} catch {
		return _cached;
	}
}

/**
 * True when no fetch was attempted in the last `intervalMs`. Throttles on the
 * last ATTEMPT, not the last usable snapshot, so an unavailable engine or a
 * failing request is retried once per interval, not on every tick, and is
 * still retried.
 */
export function processFamilyRefreshDue(
	intervalMs: number,
	monoNowMs: number = _monoNowMs()
): boolean {
	return _attemptAtMonoMs === null || monoNowMs - _attemptAtMonoMs >= intervalMs;
}

/** Monotonic age of the last applied snapshot in ms, or null if there is none. */
export function processFamilySnapshotAgeMs(monoNowMs: number = _monoNowMs()): number | null {
	if (_cachedAtMonoMs === null) return null;
	return Math.max(0, monoNowMs - _cachedAtMonoMs);
}
