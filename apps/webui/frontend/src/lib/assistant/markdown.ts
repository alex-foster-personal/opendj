/**
 * Assistant text to safe HTML: `marked` parses, DOMPurify strips anything
 * executable. Pure apart from the DOM DOMPurify needs, so the view can
 * `{@html}` the result and stay presentational.
 *
 * Links open outside the app: a model-written href must never navigate the
 * window that is playing the decks.
 */
import DOMPurify from 'dompurify';
import { Marked } from 'marked';

const parser = new Marked({ gfm: true, breaks: true, async: false });
let hooked = false;

export function renderMarkdown(text: string): string {
	return purifier().sanitize(parser.parse(text, { async: false }));
}

function purifier(): typeof DOMPurify {
	if (!DOMPurify.isSupported)
		throw new Error('renderMarkdown needs a DOM; DOMPurify is unsupported here');
	if (!hooked) {
		DOMPurify.addHook('afterSanitizeAttributes', (node) => {
			if (node.tagName !== 'A') return;
			node.setAttribute('target', '_blank');
			node.setAttribute('rel', 'noopener noreferrer');
		});
		hooked = true;
	}
	return DOMPurify;
}
