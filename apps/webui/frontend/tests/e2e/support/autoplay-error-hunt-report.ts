/**
 * Pure helpers for the AutoPlay / mixing error hunt (#1853).
 *
 * No Playwright page object: a unit test can import this module and prove
 * grouping, allowlist matching, and the "no issue number = mute button"
 * refusal without booting a browser.
 */
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

export const HUNT_REPORT_FILENAME = 'error-hunt.json';
export const HUNT_ROUTE = '/performance';
export const NEGATIVE_CONTROL_NEEDLE = 'autoplay-error-hunt negative control';

export type HuntEventKind =
	| 'pageerror'
	| 'console.error'
	| 'unhandledrejection'
	| 'toast-error'
	| 'http-4xx'
	| 'http-5xx'
	| 'stall'
	| 'silence';

export type HuntEvent = {
	ts: string;
	kind: HuntEventKind;
	message: string;
	url?: string;
	status?: number;
	method?: string;
	action: string;
	route: string;
};

export type AllowlistEntry = {
	signature: string;
	issue: string;
	reason?: string;
};

export type GroupedFinding = {
	signature: string;
	kind: HuntEventKind;
	count: number;
	first_seen_at: string;
	last_seen_at: string;
	last_scripted_action: string;
	route: string;
	examples: HuntEvent;
};

export type HuntReportStatus = 'PASS' | 'FAIL' | 'UNKNOWN';

export type HuntReport = {
	status: HuntReportStatus;
	minutes_requested: number;
	seed: number;
	started_at: string;
	ended_at: string;
	route: string;
	unknown_reason: string | null;
	findings: GroupedFinding[];
	allowlisted: GroupedFinding[];
	unexpected: GroupedFinding[];
	actions_ran: string[];
};

const ISSUE_RE = /^#\d+$/;
const LOOPBACK_ORIGIN_RE = /https?:\/\/127\.0\.0\.1:\d+/gi;
const HEX_HASH_RE = /\b[0-9a-f]{8,}\b/gi;
const ISO_TS_RE = /\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?/g;

function collapseWhitespace(value: string): string {
	return value.replace(/\s+/g, ' ').trim();
}

function stripVolatile(value: string, collapseNumbers = false): string {
	let stripped = value
		.replace(LOOPBACK_ORIGIN_RE, 'http://127.0.0.1')
		.replace(HEX_HASH_RE, '<hash>')
		.replace(ISO_TS_RE, '<ts>')
		.replace(/\?[^\s]*/g, '');
	if (collapseNumbers || /\bxrun\b/i.test(stripped)) {
		stripped = stripped.replace(/\d+(?:\.\d+)?/g, '<n>');
	}
	return collapseWhitespace(stripped);
}

function pathnameWithoutQuery(url: string): string {
	try {
		return new URL(url).pathname;
	} catch {
		const cut = url.split(/[?#]/, 1)[0];
		return cut === undefined || cut === '' ? url : cut;
	}
}

export function normalizeSignature(event: HuntEvent): string {
	if (event.kind === 'toast-error') {
		return `toast-error:${stripVolatile(event.message)}`;
	}
	if (event.kind === 'http-4xx' || event.kind === 'http-5xx') {
		const status = event.status ?? 0;
		const method = (event.method ?? 'GET').toUpperCase();
		const path = stripVolatile(pathnameWithoutQuery(event.url ?? event.route), false);
		return `http-${status}:${method}:${path}`;
	}
	if (event.kind === 'stall' || event.kind === 'silence') {
		return `${event.kind}:${stripVolatile(event.message)}`;
	}
	return `${event.kind}:${stripVolatile(event.message)}`;
}

export function groupBySignature(events: readonly HuntEvent[]): GroupedFinding[] {
	const bySignature = new Map<string, GroupedFinding>();
	for (const event of events) {
		const signature = normalizeSignature(event);
		const existing = bySignature.get(signature);
		if (existing === undefined) {
			bySignature.set(signature, {
				signature,
				kind: event.kind,
				count: 1,
				first_seen_at: event.ts,
				last_seen_at: event.ts,
				last_scripted_action: event.action,
				route: event.route,
				examples: event
			});
			continue;
		}
		existing.count += 1;
		existing.last_seen_at = event.ts;
		existing.last_scripted_action = event.action;
		existing.route = event.route;
	}
	return [...bySignature.values()];
}

export function loadAllowlist(path: string): AllowlistEntry[] {
	const raw: unknown = JSON.parse(readFileSync(path, 'utf-8'));
	if (typeof raw !== 'object' || raw === null || !Array.isArray((raw as { entries?: unknown }).entries)) {
		throw new Error(`allowlist at ${path} must be { "entries": [...] }`);
	}
	const entries: AllowlistEntry[] = [];
	for (const item of (raw as { entries: unknown[] }).entries) {
		if (typeof item !== 'object' || item === null) {
			throw new Error(`allowlist at ${path} has a non-object entry`);
		}
		const row = item as Record<string, unknown>;
		if (typeof row.signature !== 'string' || row.signature.length === 0) {
			throw new Error(`allowlist at ${path} has an entry with no signature`);
		}
		if (typeof row.issue !== 'string' || !ISSUE_RE.test(row.issue)) {
			throw new Error(
				`allowlist at ${path} refuses an entry with no open-issue number ` +
					`(got ${JSON.stringify(row.issue)}); that would be a mute button`
			);
		}
		if (row.signature.includes(NEGATIVE_CONTROL_NEEDLE)) {
			throw new Error(
				`allowlist at ${path} must not mute the negative-control signature: ${row.signature}`
			);
		}
		const entry: AllowlistEntry = { signature: row.signature, issue: row.issue };
		if (typeof row.reason === 'string') entry.reason = row.reason;
		entries.push(entry);
	}
	return entries;
}

export function unexpectedSignatures(
	grouped: readonly GroupedFinding[],
	allowlist: readonly AllowlistEntry[]
): GroupedFinding[] {
	const allowed = new Set(allowlist.map((entry) => entry.signature));
	return grouped.filter((finding) => !allowed.has(finding.signature));
}

export function allowlistedSignatures(
	grouped: readonly GroupedFinding[],
	allowlist: readonly AllowlistEntry[]
): GroupedFinding[] {
	const allowed = new Set(allowlist.map((entry) => entry.signature));
	return grouped.filter((finding) => allowed.has(finding.signature));
}

export function formatReport(grouped: readonly GroupedFinding[]): string {
	if (grouped.length === 0) {
		return 'autoplay-error-hunt: no findings';
	}
	const lines = ['autoplay-error-hunt findings (deduplicated by signature):'];
	for (const finding of grouped) {
		lines.push(
			`  ${finding.signature}  count=${finding.count}  first_seen=${finding.first_seen_at}  ` +
				`last_action=${finding.last_scripted_action}  route=${finding.route}`
		);
	}
	return lines.join('\n');
}

export function writeHuntReport(report: HuntReport, directories: readonly string[]): string[] {
	const payload = `${JSON.stringify(report, null, 2)}\n`;
	const written: string[] = [];
	for (const directory of directories) {
		mkdirSync(directory, { recursive: true });
		const path = join(directory, HUNT_REPORT_FILENAME);
		writeFileSync(path, payload, 'utf-8');
		written.push(path);
	}
	return written;
}

export function buildHuntReport(input: {
	minutesRequested: number;
	seed: number;
	startedAt: string;
	endedAt: string;
	unknownReason: string | null;
	events: readonly HuntEvent[];
	allowlist: readonly AllowlistEntry[];
	actionsRan: readonly string[];
}): HuntReport {
	const findings = groupBySignature(input.events);
	const allowlisted = allowlistedSignatures(findings, input.allowlist);
	const unexpected = unexpectedSignatures(findings, input.allowlist);
	let status: HuntReportStatus = 'PASS';
	if (input.unknownReason !== null) status = 'UNKNOWN';
	else if (unexpected.length > 0) status = 'FAIL';
	return {
		status,
		minutes_requested: input.minutesRequested,
		seed: input.seed,
		started_at: input.startedAt,
		ended_at: input.endedAt,
		route: HUNT_ROUTE,
		unknown_reason: input.unknownReason,
		findings,
		allowlisted,
		unexpected,
		actions_ran: [...input.actionsRan]
	};
}
