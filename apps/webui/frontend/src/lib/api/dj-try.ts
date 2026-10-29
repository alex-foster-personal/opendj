/**
 * Transition try-er client (TRYER-10/13): its own module rather than the `$lib/api` barrel, so the panel
 * adds no importer to the barrel. Responses are the generated TryJobOut schema, so no cast.
 */
import type { components } from '$lib/api-types';
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

export async function pickCandidate(
	job: TryJobOut,
	candidateId: string
): Promise<TryJobOut> {
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
