import { hiddenBrokenLabel } from './playlist-folder-labels';

import './PlaylistHiddenBrokenNotice.react.css';

// React twin of PlaylistHiddenBrokenNotice.svelte: same DOM, held to
// PlaylistHiddenBrokenNotice.contract.ts.
export type PlaylistHiddenBrokenNoticeProps = { hiddenBrokenPlaylistCount: number };

export default function PlaylistHiddenBrokenNotice({ hiddenBrokenPlaylistCount }: PlaylistHiddenBrokenNoticeProps) {
	if (!(hiddenBrokenPlaylistCount > 0)) return null;
	return (
		<div className="row child rb-inert" data-testid="playlists-hidden-broken">
			{hiddenBrokenLabel(hiddenBrokenPlaylistCount)}
		</div>
	);
}
