/**
 * CURRENT fold control state (pin 2ac3a0): mirrors TrackTable's
 * MASTER-with-chevron fold (masterFold/jumpToMaster) - when the selected
 * playlist row scrolls outside the tree's own viewport, a sticky blue
 * CURRENT control with an up/down chevron appears at the corresponding
 * edge so a library with MANY playlists never loses track of which one
 * is the active pane's selection.
 *
 * Split out of PlaylistTree.svelte's script: a self-contained concern
 * (DOM scroll/position tracking) distinct from the tree's row rendering.
 * PlaylistTree still owns the scrollable container and the row elements
 * themselves (they render the rest of the tree), so this tracker exposes
 * its state as plain class fields that PlaylistTree wires up via
 * `bind:this={tracker.scrollEl}` / `bind:clientHeight={tracker.viewportHeight}`
 * / `onscroll={tracker.onScroll}` / `use:tracker.bindSelectedRow={...}`,
 * same DOM hookup as before the split, just owned here instead of inline.
 *
 * Rune class - the .svelte.ts extension is REQUIRED for $state/$derived.
 */
import { computeTreeCurrentFold } from './playlist-context';

export class TreeFoldTracker {
	scrollEl: HTMLDivElement | null = $state(null);
	selectedRowEl: HTMLElement | null = $state(null);
	scrollTop = $state(0);
	viewportHeight = $state(0);

	/** Tracks whichever child row is the current playlist selection - the
	 * selected row is unique, so the last (selected) call to fire wins and the
	 * teardown call from every non-selected row is a harmless no-op. */
	bindSelectedRow = (el: HTMLElement, isSelected: boolean) => {
		if (isSelected) this.selectedRowEl = el;
		return {
			update: (nowSelected: boolean) => {
				if (nowSelected) this.selectedRowEl = el;
				else if (this.selectedRowEl === el) this.selectedRowEl = null;
			},
			destroy: () => {
				if (this.selectedRowEl === el) this.selectedRowEl = null;
			}
		};
	};

	onScroll = (event: Event): void => {
		this.scrollTop = (event.currentTarget as HTMLDivElement).scrollTop;
	};

	readonly current = $derived.by((): 'above' | 'below' | null => {
		const scrollEl = this.scrollEl;
		const rowEl = this.selectedRowEl;
		if (scrollEl === null || rowEl === null) return null;
		const containerTop = scrollEl.getBoundingClientRect().top;
		const rowRect = rowEl.getBoundingClientRect();
		const top = rowRect.top - containerTop + this.scrollTop;
		const bottom = top + rowRect.height;
		return computeTreeCurrentFold({
			selectedTop: top,
			selectedBottom: bottom,
			scrollTop: this.scrollTop,
			viewportHeight: this.viewportHeight
		});
	});

	jumpToCurrent(): void {
		this.selectedRowEl?.scrollIntoView({ block: 'center' });
	}
}
