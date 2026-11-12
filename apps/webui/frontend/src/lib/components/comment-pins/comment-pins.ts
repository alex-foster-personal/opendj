/**
 * Entry for `<comment-pins>` (issue #11295). Browser code lives in
 * `comment-pins-element.ts` so node:test can load the API without HTMLElement.
 */
export { COMMENT_PINS_TAG } from './comment-pins-constants';
export {
	CommentPinsApi,
	commentPathsForPrefix,
	type CommentPin,
	type CommentCreateIn
} from './comment-pins-api';
export { CommentPinsController } from './comment-pins-controller';

export function defineCommentPins(): void {
	if (typeof HTMLElement === 'undefined' || typeof customElements === 'undefined') return;
	// Dynamic import keeps the HTMLElement subclass off the node:test bundle path
	// unless something actually registers the element in a browser.
	void import('./comment-pins-element').then(({ defineCommentPinsElement }) =>
		defineCommentPinsElement()
	);
}

export function bootCommentPinsElement(): void {
	defineCommentPins();
}
