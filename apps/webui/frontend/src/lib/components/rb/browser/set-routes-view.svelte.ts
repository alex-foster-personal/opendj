/**
 * SET-6586 Routes view state shared by the set tabs, the route list and the
 * browser pane: whether Routes mode is on, which route is lit, and the sets the
 * tabs loaded. One instance, reset whenever the pane shows another playlist.
 */
import type { PlaylistSet } from '$lib/rb/api-playlist-sets';
import type { RouteComponent, RoutePick } from './set-routes';

class SetRoutesView {
	playlistId = $state<string | null>(null);
	/** Routes mode: the route list replaces the track table. */
	active = $state(false);
	/** The lit route; null until the components load and pick a default. */
	pick = $state<RoutePick | null>(null);
	/** The sets the tabs loaded for `playlistId`. */
	sets = $state<PlaylistSet[]>([]);
	/** The set tab last clicked, so the route bar can move that set's route. */
	selectedSetId = $state<number | null>(null);
	/** A set was clicked before the components loaded; its route is picked once they do. */
	pickPending = $state(false);
	/** Written by the lazy SetRoutesTools: the playlist's components, each one's
	 *  tab glyph, and the set whose route is lit (null when Routes is off). */
	components = $state<RouteComponent[]>([]);
	glyphs = $state<ReadonlyMap<number, string>>(new Map());
	routeSetId = $state<number | null>(null);

	/** Point at a playlist; a different one clears the route but keeps the mode. */
	show(playlistId: string): void {
		if (this.playlistId === playlistId) return;
		this.playlistId = playlistId;
		this.pick = null;
		this.sets = [];
		this.selectedSetId = null;
		this.pickPending = false;
		this.components = [];
		this.glyphs = new Map();
		this.routeSetId = null;
	}
}

export const setRoutesView = new SetRoutesView();
