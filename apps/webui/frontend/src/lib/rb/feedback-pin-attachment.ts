/**
 * Client-side policy for pasted comment-pin screenshots (issue #1333, part 2
 * of #928). Split into its own module rather than feedback.ts, which is
 * already at the 600-line file-size gate.
 *
 * MAX_PIN_ATTACHMENT_BYTES and ALLOWED_PIN_ATTACHMENT_TYPES mirror
 * apps/webui/server/routes/feedback_attachments.py so a paste can be refused
 * immediately, before a round trip, with the same message the server would
 * give. The server is the enforced source of truth either way: a drift
 * between the two only costs a slower refusal (caught server-side instead),
 * never a wrong one.
 */

export const MAX_PIN_ATTACHMENT_BYTES = 8 * 1024 * 1024;

export const ALLOWED_PIN_ATTACHMENT_TYPES: ReadonlySet<string> = new Set([
	'image/png',
	'image/jpeg',
	'image/gif',
	'image/webp'
]);

/** The three distinguishable outcomes of a clipboard paste (review fixup, PR
 * #1425 P2): no image item at all (quietly do nothing - it might be a plain
 * text paste), an image item whose type is not on the allowed list (refuse
 * with an explicit message - FB-11 forbids a silent drop), or an accepted
 * image file. */
export type PastedImageOutcome =
	| { kind: 'none' }
	| { kind: 'rejected'; type: string }
	| { kind: 'file'; file: File };

/** Classifies the first image-shaped clipboard item (an item whose MIME type
 * starts with `image/`), or reports no image item was present at all. Plain-
 * DOM-shape input (not `ClipboardEvent` itself) so this stays unit-testable
 * without a browser. */
export function pastedImageFile(
	items: { type: string; getAsFile(): File | null }[] | undefined | null
): PastedImageOutcome {
	if (items === undefined || items === null) return { kind: 'none' };
	let rejectedType: string | null = null;
	for (const item of items) {
		if (!item.type.startsWith('image/')) continue;
		if (!ALLOWED_PIN_ATTACHMENT_TYPES.has(item.type)) {
			if (rejectedType === null) rejectedType = item.type;
			continue;
		}
		const file = item.getAsFile();
		if (file !== null) return { kind: 'file', file };
	}
	if (rejectedType !== null) return { kind: 'rejected', type: rejectedType };
	return { kind: 'none' };
}

function _mb(bytes: number): string {
	return (bytes / (1024 * 1024)).toFixed(1);
}

/** Explicit refusal text for an oversized paste, or null when it is within
 * policy - never a silent drop. */
export function attachmentSizeRefusal(file: File): string | null {
	if (file.size <= MAX_PIN_ATTACHMENT_BYTES) return null;
	return `screenshot is ${_mb(file.size)}MB; the limit is ${_mb(MAX_PIN_ATTACHMENT_BYTES)}MB`;
}
