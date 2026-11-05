/**
 * "Why this track" and the one-tap verdict (ADR-0139): the AutoPlay menu reads the stored decision record and posts
 * the listener's good / meh / bad. Pure logging: nothing here reaches the planner. CLI twins: `opendj dj why`,
 * `opendj dj verdict`.
 */

import { api, readApiErrorStatus, unwrap } from '$lib/api/client';

export const LAST_URL = '/api/v1/dj/transitions/last' as const;
export const VERDICT_URL = '/api/v1/dj/transitions/verdict' as const;
export const VERDICTS = ['good', 'meh', 'bad'] as const;
export type Verdict = (typeof VERDICTS)[number];

export interface WhyCandidate {
	cid: string;
	track: string;
	style: string;
	bars: number;
	score: number;
	warnings: string[];
}

export interface WhyRecord {
	out_track_id: string;
	in_track_id: string;
	decided_at: string;
	decision: {
		reason: string;
		shortlist: string[];
		rejected: { track: string; reason: string }[];
		candidates: WhyCandidate[];
		chosen: string | null;
		/** The intent and the styles already played that the chooser weighed (absent on records stored before them). */
		intent?: string | null;
		recent_styles?: string[];
	};
	played: { style: string; times_played: number } | null;
	verdict: Verdict | null;
	verdict_at: string | null;
}

/** The newest stored decision, null when none is stored yet (404). Any other failure throws, with the server's text.
 * Through the shared API client, so a remote daemon (`VITE_API_BASE`) is asked, not the page's own origin.
 * `fetchImpl` is the tests' seam. */
export async function fetchLastDecision(fetchImpl?: typeof fetch): Promise<WhyRecord | null> {
	try {
		const body = await unwrap(
			api.GET(LAST_URL, fetchImpl === undefined ? {} : { fetch: fetchImpl })
		);
		return body as unknown as WhyRecord;
	} catch (error) {
		if (readApiErrorStatus(error) === 404) return null;
		throw new Error(`why this track: ${error instanceof Error ? error.message : String(error)}`);
	}
}

export async function postVerdict(
	record: Pick<WhyRecord, 'out_track_id' | 'in_track_id'>,
	verdict: Verdict,
	fetchImpl?: typeof fetch
): Promise<void> {
	try {
		await unwrap(
			api.POST(VERDICT_URL, {
				body: { out: record.out_track_id, in: record.in_track_id, verdict },
				...(fetchImpl === undefined ? {} : { fetch: fetchImpl })
			})
		);
	} catch (error) {
		throw new Error(`verdict: ${error instanceof Error ? error.message : String(error)}`);
	}
}

/** The verdict is offered once the decided pair has played and nobody has judged it yet. */
export function verdictOffered(record: WhyRecord | null): boolean {
	return (
		record !== null &&
		record.in_track_id !== '' &&
		record.played !== null &&
		record.verdict === null
	);
}

/** The chosen candidate, or null when the decision chose none. */
export function chosenCandidate(record: WhyRecord): WhyCandidate | null {
	return record.decision.candidates.find((c) => c.cid === record.decision.chosen) ?? null;
}
