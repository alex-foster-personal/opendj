/**
 * Transition try-er client (TRYER-10/13): its own module rather than the `$lib/api` barrel, so the panel
 * adds no importer to the barrel. Responses are the generated TryJobOut schema, so no cast.
 */
import type { components } from '$lib/api-types';
import { refuseStickRead } from '$lib/rb/track-source';
import { api, unwrap } from './client';

type TryJobOut = components['schemas']['TryJobOut'];

export async function startTry(
	outId: string,
	inId: string,
	n: number,
	contextBars: number
): Promise<TryJobOut> {
	return await unwrap(
		api.POST('/api/v1/dj/try', {
			body: { out: outId, in: inId, n, context_bars: contextBars }
		})
	);
}

export async function pickCandidate(job: TryJobOut, candidateId: string): Promise<TryJobOut> {
	return await unwrap(
		api.POST('/api/v1/dj/try/{job_id}/pick', {
			params: {
				path: { job_id: job.job_id },
				header: { 'if-match': job.etag }
			},
			body: { candidate: candidateId }
		})
	);
}

export async function undoPick(job: TryJobOut): Promise<TryJobOut> {
	return await unwrap(
		api.DELETE('/api/v1/dj/try/{job_id}/pick', {
			params: {
				path: { job_id: job.job_id },
				header: { 'if-match': job.etag }
			}
		})
	);
}

/** Both decks' waveforms and knob lines for one candidate's render (TRYER-16). */
export async function getLanes(jobId: string, candidateId: string, columns: number) {
	return await unwrap(
		api.GET('/api/v1/dj/try/{job_id}/candidates/{candidate_id}/lanes', {
			params: { path: { job_id: jobId, candidate_id: candidateId }, query: { columns } }
		})
	);
}

export type TryLanes = Awaited<ReturnType<typeof getLanes>>;

/** "artist - title" for a try-er track (TRYER-16). Library tracks only: the try-er resolves audio from the library,
 *  so a USB stick id is refused here rather than sent to the library route. */
export async function trackLabel(stableId: string): Promise<string> {
	refuseStickRead(stableId, 'name a try-er track');
	const track = await unwrap(
		api.GET('/api/v1/tracks/{stable_id}', { params: { path: { stable_id: stableId } } })
	);
	return `${track.artist ?? 'unknown artist'} - ${track.title ?? stableId}`;
}
