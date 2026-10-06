/**
 * Release a fetch Response whose body the caller will never read.
 *
 * P0, Tue 6 Oct 2026 (Open DJ 1.0 alpha, packaged build 17 on WKWebView): the
 * WebContent process grew 27 MB/min with every deck paused and 84 MB/min with
 * four decks loaded, and a heap snapshot with EMPTY decks held 2,243 live
 * fetch Response objects and 2,951 AbortSignals. The 1 Hz UI-mirror PUT (and
 * the other fire-and-forget posts and polls) never read or cancelled the
 * body of a successful or non-2xx answer. WebKit keeps a Response whose body
 * stream is still open alive, together with its Request (and that Request's
 * signal and serialized body, which is the whole mirror document), so every
 * tick leaked one.
 *
 * Every fetch whose body is not consumed MUST pass through this. A body that
 * is already used or locked by a reader is left alone: its reader owns it.
 */
export function releaseBody(response: Response): void {
	const body = response.body;
	if (body === null || response.bodyUsed || body.locked) return;
	body.cancel().catch((error: unknown) => {
		console.warn(`releaseBody: cancelling an unread ${response.status} body failed`, error);
	});
}
