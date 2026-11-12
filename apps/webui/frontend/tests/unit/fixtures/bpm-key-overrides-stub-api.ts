/**
 * Stub key-edit API for the library cell commit tests (KEY-10229): records each
 * call and answers with the canonical key the test sets, or rejects.
 */

export type StubApiCall = { method: string; path: string; body?: unknown };

export const apiCalls: StubApiCall[] = [];
let answer: { key: string | null } | Error = { key: null };

export function apiAnswerWith(next: { key: string | null } | Error): void {
	answer = next;
	apiCalls.length = 0;
}

async function reply(stableId: string) {
	if (answer instanceof Error) throw answer;
	return { stable_id: stableId, key: answer.key, modified_at: '2026-11-09T00:00:00Z' };
}

export async function putKeyEdit(stableId: string, key: string) {
	apiCalls.push({ method: 'PUT', path: `/api/v1/tracks/${stableId}/key-edit`, body: { key } });
	return reply(stableId);
}

export async function deleteKeyEdit(stableId: string) {
	apiCalls.push({ method: 'DELETE', path: `/api/v1/tracks/${stableId}/key-edit` });
	return { stable_id: stableId, key: null, modified_at: '2026-11-09T00:00:00Z' };
}
