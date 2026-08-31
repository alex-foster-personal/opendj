/**
 * Purpose-made drag image for library row drags.
 *
 * Without an explicit setDragImage the browser snapshots the dragged element as
 * it sits on screen. In WebKit that snapshot picks up overlapping composited
 * layers too, so dragging one table row visibly drags chunks of unrelated UI
 * along with it. A small detached element is built here and snapshotted
 * instead: one line of track identity, plus a count when the drag is a
 * multi-select.
 *
 * The element must be attached and laid out at setDragImage time, hence the
 * offscreen body append; it is removed on dragend (and defensively on the next
 * dragstart) so nothing accumulates.
 */

/** Cursor-to-ghost offset. Small, so the ghost tracks the pointer closely. */
const GHOST_OFFSET_X = 14;
const GHOST_OFFSET_Y = 12;

export interface TrackDragGhostSpec {
	title: string;
	artist: string;
	/** Tracks in the drag; the count badge renders only when this is > 1. */
	count: number;
}

/** The live ghost, held by identity so removal never guesses from the DOM. */
let _ghost: HTMLElement | null = null;

/** Palette matches the deck/browser chrome; the ghost renders outside
 * .perf-root, so the --rb-* fallbacks are what actually paint. */
const ROOT_CSS = [
	'position: fixed',
	'top: -10000px',
	// Horizontally on-screen on purpose: WebKit will not snapshot an element
	// parked outside the viewport on BOTH axes.
	'left: 0',
	'pointer-events: none',
	'display: flex',
	'align-items: center',
	'gap: 8px',
	'max-width: 320px',
	'padding: 5px 9px',
	'border: 1px solid color-mix(in srgb, var(--rb-accent, #6af) 55%, #555)',
	'border-radius: 4px',
	'background: var(--rb-panel, #141820)',
	'color: var(--rb-text, #dde3ea)',
	'font-family: var(--rb-font, system-ui)',
	'font-size: 11px',
	'line-height: 1.3',
	'white-space: nowrap'
].join(';');

const TEXT_CSS = 'overflow: hidden; text-overflow: ellipsis; max-width: 190px';
const ARTIST_CSS = `${TEXT_CSS}; color: var(--rb-text-dim, #9aa)`;
const COUNT_CSS = [
	'flex: none',
	'padding: 1px 6px',
	'border-radius: 8px',
	'background: #ff4da6',
	'color: #fff',
	'font-size: 10px'
].join(';');

export function buildTrackDragGhost(doc: Document, spec: TrackDragGhostSpec): HTMLElement {
	const root = doc.createElement('div');
	root.className = 'mdt-drag-ghost';
	root.setAttribute('aria-hidden', 'true');
	root.style.cssText = ROOT_CSS;

	const title = doc.createElement('span');
	title.style.cssText = TEXT_CSS;
	title.textContent = spec.title;
	root.appendChild(title);

	if (spec.artist !== '') {
		const artist = doc.createElement('span');
		artist.style.cssText = ARTIST_CSS;
		artist.textContent = spec.artist;
		root.appendChild(artist);
	}

	if (spec.count > 1) {
		const count = doc.createElement('span');
		count.style.cssText = COUNT_CSS;
		count.textContent = `${spec.count} tracks`;
		root.appendChild(count);
	}
	return root;
}

/** Call from dragstart, after setData. No-op when the event carries no
 * dataTransfer, so a ghost is never appended without a drag to attach it to. */
export function installTrackDragGhost(event: DragEvent, spec: TrackDragGhostSpec): void {
	if (event.dataTransfer === null) return;
	removeTrackDragGhost();
	const ghost = buildTrackDragGhost(document, spec);
	document.body.appendChild(ghost);
	_ghost = ghost;
	event.dataTransfer.setDragImage(ghost, GHOST_OFFSET_X, GHOST_OFFSET_Y);
}

export function removeTrackDragGhost(): void {
	_ghost?.remove();
	_ghost = null;
}
