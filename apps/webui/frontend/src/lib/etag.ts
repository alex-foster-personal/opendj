/** Client-side helper -- the server computes the authoritative ETag. */
export function extractEtag(headers: Headers): string {
	return headers.get('etag') ?? '';
}
