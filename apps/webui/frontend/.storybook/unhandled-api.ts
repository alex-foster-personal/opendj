// A daemon request no story handler answers fails the story itself, not only
// the request. MSW's print.error() rejects the fetch, but a component that
// catches the rejection renders its normal error state, so a contract whose
// handler was removed or whose URL drifted could still pass on that state.
// Both previews (.storybook and .storybook-react) record every unhandled
// /api/ request here and fail the story in afterEach. A recorded replay whose
// query no longer matches (src/sandbox/replay.ts) fails the story the same way.

import { takeReplayDrift } from '../src/sandbox/replay';

const seen: string[] = [];

export function onUnhandledApiRequest(request: Request, print: { error: () => void }): void {
	const url = new URL(request.url);
	if (!url.pathname.startsWith('/api/')) return;
	seen.push(`${request.method} ${url.pathname}${url.search}`);
	print.error();
}

export function resetUnhandledApi(): void {
	seen.length = 0;
	takeReplayDrift();
}

export function failOnUnhandledApi(): void {
	const requests = seen.splice(0);
	const drifted = takeReplayDrift();
	if (requests.length > 0) {
		throw new Error(`story made API request(s) no handler answers: ${requests.join(', ')}`);
	}
	if (drifted.length > 0) {
		throw new Error(
			`story's API request(s) drifted from the recorded replay: ${drifted.join('; ')}`
		);
	}
}
