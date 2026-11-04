// SPOT-6555: the Spotify account panel's pure copy and checks. No import here
// reaches the backend, so the props-only SpotifySourcePanelView can use them.
import type { SpotifyNotConnectedReason } from './spotify-account-api';

/** A Spotify app Client ID as the developer dashboard shows it: 32 hex digits. */
export function isSpotifyClientId(value: string): boolean {
	return /^[0-9a-f]{32}$/i.test(value.trim());
}

/** Human text for each not-connected reason; the panel shows it under the button. */
export function spotifyNotConnectedText(reason: SpotifyNotConnectedReason): string {
	switch (reason) {
		case 'spotify_extra_missing':
			return 'Spotify support is not installed in this engine.';
		case 'no_client_id':
			return 'Spotify needs a Client ID once before you can sign in.';
		case 'not_signed_in':
			return 'Sign in to list all your Spotify playlists.';
		case 'scope_missing':
			return 'Your Spotify sign-in cannot read private playlists. Connect again.';
		case 'token_refresh_failed':
			return 'Your Spotify sign-in expired. Connect again.';
	}
}
