import './PlaylistFolderStates.react.css';

// React twin of PlaylistFolderStates.svelte, held to the same contract
// (PlaylistFolderStates.contract.ts).
export type PlaylistFolderStatesProps = {
	nodeCount: number;
	playlistsLoading: boolean;
	playlistsError: string | null;
	hiddenBrokenPlaylistCount: number;
	/** undefined = creation isn't wired; renders plain inert text instead of
	 * an unusable button (same absent-means-inert convention as PlaylistTree). */
	oncreate: (() => void) | undefined;
};

export default function PlaylistFolderStates({
	nodeCount,
	playlistsLoading,
	playlistsError,
	hiddenBrokenPlaylistCount,
	oncreate
}: PlaylistFolderStatesProps) {
	if (nodeCount === 0 && playlistsLoading) {
		return (
			<div className="row child rb-inert" data-testid="playlists-loading">
				Loading playlists...
			</div>
		);
	}
	if (nodeCount === 0 && playlistsError !== null) {
		return (
			<div className="row child rb-inert" data-testid="playlists-error" title={playlistsError}>
				Playlist load failed
			</div>
		);
	}
	if (nodeCount === 0 && hiddenBrokenPlaylistCount === 0) {
		if (oncreate) {
			return (
				<button type="button" className="row child empty-cta" data-testid="playlists-empty-cta" onClick={() => oncreate()}>
					+ Create your first playlist
				</button>
			);
		}
		return (
			<div className="row child rb-inert" data-testid="playlists-empty">
				no playlists yet
			</div>
		);
	}
	return null;
}
