/**
 * Last-played cell for the performance track table (pin 9e8e7ff7c4ed).
 *
 * The cell reads "x days ago" from the newest source timestamp we actually
 * have. The hover lists every source that recorded a play. Rekordbox's
 * imported count often has no date, so that source shows its count instead
 * of a day. A stick row's own play count is Rekordbox USB (`usb-` ids, the
 * same prefix as `isUsbTrackId`). Combined `play_count` is not a source.
 */

export const PLAY_SOURCES = [
	'rekordbox',
	'gig',
	'rekordbox_usb',
	'serato',
	'djay',
	'traktor'
] as const;

export type PlaySourceId = (typeof PLAY_SOURCES)[number];

export interface PlayFacts {
	counts: Record<PlaySourceId, number>;
	at: Record<PlaySourceId, string | null>;
}

export interface PlayHit {
	id: PlaySourceId;
	label: string;
	detail: string;
}

const LABELS: Record<PlaySourceId, string> = {
	rekordbox: 'Rekordbox',
	gig: 'Gig',
	rekordbox_usb: 'Rekordbox USB',
	serato: 'Serato',
	djay: 'djay',
	traktor: 'Traktor'
};

const SOURCE_NAMES: Record<string, PlaySourceId> = {
	rekordbox: 'rekordbox',
	rb: 'rekordbox',
	gig: 'gig',
	odj: 'gig',
	opendj: 'gig',
	'open-dj': 'gig',
	open_dj: 'gig',
	rekordbox_usb: 'rekordbox_usb',
	'rekordbox-usb': 'rekordbox_usb',
	usb: 'rekordbox_usb',
	rb_usb: 'rekordbox_usb',
	'rb-usb': 'rekordbox_usb',
	serato: 'serato',
	djay: 'djay',
	traktor: 'traktor'
};

const COUNT_KEYS: Record<string, PlaySourceId> = {
	rekordbox_play_count: 'rekordbox',
	opendj_play_count: 'gig',
	rekordbox_usb_play_count: 'rekordbox_usb',
	usb_play_count: 'rekordbox_usb',
	serato_play_count: 'serato',
	djay_play_count: 'djay',
	traktor_play_count: 'traktor'
};

const AT_KEYS: Record<string, PlaySourceId> = {
	rekordbox_last_played_at: 'rekordbox',
	opendj_last_played_at: 'gig',
	rekordbox_usb_last_played_at: 'rekordbox_usb',
	usb_last_played_at: 'rekordbox_usb',
	serato_last_played_at: 'serato',
	djay_last_played_at: 'djay',
	traktor_last_played_at: 'traktor'
};

const DAY_MS = 86_400_000;

function blank(): PlayFacts {
	return {
		counts: {
			rekordbox: 0,
			gig: 0,
			rekordbox_usb: 0,
			serato: 0,
			djay: 0,
			traktor: 0
		},
		at: {
			rekordbox: null,
			gig: null,
			rekordbox_usb: null,
			serato: null,
			djay: null,
			traktor: null
		}
	};
}

function countOf(value: unknown): number {
	if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return 0;
	return Math.min(Math.floor(value), 1_000_000_000);
}

function dateOf(value: unknown): string | null {
	if (typeof value !== 'string' || value.trim() === '') return null;
	return Number.isFinite(Date.parse(value)) ? value : null;
}

function sourceId(value: unknown): PlaySourceId | null {
	if (typeof value !== 'string') return null;
	const key = value.trim().toLowerCase().replaceAll(' ', '_');
	return SOURCE_NAMES[key] ?? null;
}

function putCount(facts: PlayFacts, id: PlaySourceId, count: number): void {
	if (count > facts.counts[id]) facts.counts[id] = count;
}

function putDate(facts: PlayFacts, id: PlaySourceId, iso: string | null): void {
	if (iso === null) return;
	const prev = facts.at[id];
	if (prev !== null && Date.parse(prev) >= Date.parse(iso)) return;
	facts.at[id] = iso;
}

function absorbList(facts: PlayFacts, value: unknown): boolean {
	if (!Array.isArray(value)) return false;
	let any = false;
	for (const item of value) {
		if (item === null || typeof item !== 'object') continue;
		const rec = item as Record<string, unknown>;
		const id =
			sourceId(rec.source) ??
			sourceId(rec.where) ??
			sourceId(rec.kind) ??
			sourceId(rec.vendor) ??
			sourceId(rec.id) ??
			sourceId(rec.name);
		if (id === null) continue;
		const count = countOf(rec.count) || countOf(rec.play_count) || countOf(rec.plays);
		const at = dateOf(rec.played_at) ?? dateOf(rec.last_played_at) ?? dateOf(rec.at);
		if (count <= 0 && at === null) continue;
		putCount(facts, id, count);
		putDate(facts, id, at);
		any = true;
	}
	return any;
}

/** Known source fields on a listing row or `GET /tracks/{id}/plays`. Null when none. */
export function playFactsFromUnknown(value: unknown): PlayFacts | null {
	if (value === null || typeof value !== 'object') return null;
	const obj = value as Record<string, unknown>;
	const facts = blank();
	let any = false;
	for (const [key, id] of Object.entries(COUNT_KEYS)) {
		const count = countOf(obj[key]);
		if (count <= 0) continue;
		putCount(facts, id, count);
		any = true;
	}
	for (const [key, id] of Object.entries(AT_KEYS)) {
		const at = dateOf(obj[key]);
		if (at === null) continue;
		putDate(facts, id, at);
		any = true;
	}
	if (absorbList(facts, obj.sources)) any = true;
	if (absorbList(facts, obj.play_sources)) any = true;
	return any ? facts : null;
}

export function withUsbRow(facts: PlayFacts, playCount: number): PlayFacts {
	const count = countOf(playCount);
	if (count <= 0 || count <= facts.counts.rekordbox_usb) return facts;
	return {
		counts: { ...facts.counts, rekordbox_usb: count },
		at: facts.at
	};
}

/** Wire facts, then a fetched plays payload, then a stick row's own count. */
export function displayFacts(input: {
	stableId: string;
	playCount: number;
	wire: PlayFacts | null;
	fetched: PlayFacts | null;
}): PlayFacts {
	const next = input.fetched ?? input.wire ?? blank();
	if (input.stableId.startsWith('usb-')) return withUsbRow(next, input.playCount);
	return next;
}

/** Elapsed whole days. Same day and a future timestamp both read "today". */
export function daysAgoLabel(iso: string | null, nowMs: number): string {
	if (iso === null) return '';
	const then = Date.parse(iso);
	if (!Number.isFinite(then) || !Number.isFinite(nowMs)) return '';
	const days = Math.floor((nowMs - then) / DAY_MS);
	if (days <= 0) return 'today';
	if (days === 1) return '1 day ago';
	return `${days} days ago`;
}

export function lastPlayedLabel(facts: PlayFacts, nowMs: number): string {
	let best = -Infinity;
	let bestIso: string | null = null;
	for (const id of PLAY_SOURCES) {
		const iso = facts.at[id];
		if (iso === null) continue;
		const then = Date.parse(iso);
		if (!Number.isFinite(then) || then < best) continue;
		best = then;
		bestIso = iso;
	}
	return daysAgoLabel(bestIso, nowMs);
}

export function playHits(facts: PlayFacts, nowMs: number): PlayHit[] {
	const hits: PlayHit[] = [];
	for (const id of PLAY_SOURCES) {
		const count = facts.counts[id];
		const at = facts.at[id];
		if (count <= 0 && at === null) continue;
		const dated = at === null ? '' : daysAgoLabel(at, nowMs);
		const detail =
			dated !== '' ? dated : count === 1 ? '1 play' : count > 1 ? `${count} plays` : 'played';
		hits.push({ id, label: LABELS[id], detail });
	}
	return hits;
}
