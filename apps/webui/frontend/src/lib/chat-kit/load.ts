/**
 * Lazy entry for the Open DJ chat kit presentational components (#11169).
 *
 * Callers static-import only this module (and types) so unused routes pay for
 * this loader, not the Svelte components. Conversation chrome mounts after
 * `loadChatKit()` resolves.
 */
import type { ChatKitComponents } from './types';

export type { CitationRef, CitationState, ChatKitComponents, StreamingBubbleRole } from './types';
export { appendTextDelta, CITATION_STATES, citationStateTitle } from './types';

let chatKitModule: Promise<ChatKitComponents> | null = null;

/** Fetch the chat-kit Svelte components as one shared dynamic chunk. */
export function loadChatKit(): Promise<ChatKitComponents> {
	if (chatKitModule === null) {
		chatKitModule = Promise.all([
			import('../components/chat-kit/CitationPill.svelte'),
			import('../components/chat-kit/StreamingBubble.svelte')
		]).then(([citation, streaming]) => ({
			CitationPill: citation.default,
			StreamingBubble: streaming.default
		}));
	}
	return chatKitModule;
}

/** Unit tests only: clear the cached import promise between cases. */
export function resetChatKitLoadForTests(): void {
	chatKitModule = null;
}
