# Spotify playlist importer (Phase 9 / CAT-01)

Reads a Spotify playlist via the user's own account, matches each track
against the local library in the shared-state layer, and emits CSV +
Markdown artifacts plus (in live mode) inserts playlist + memberships
+ pending rows into the state DB.

No DRM bypass, no audio downloads, no yt-dlp. Purchase path stays
manual: unmatched tracks land in an "acquisition queue" review sheet
with deep-link search URLs for Beatport, Bandcamp, Qobuz, Apple Music,
and Discogs.

## One-time setup

1. Create a Spotify developer app: <https://developer.spotify.com/dashboard>.
2. Add `http://127.0.0.1:8888/callback` as a redirect URI.
3. Stash the Client ID in Doppler (no client secret needed; PKCE):

   ```sh
   doppler secrets set -p construct -c dev_af SPOTIFY_CLIENT_ID=<id>
   ```

## Usage

Dry run first; review `data/spotify/import-<id>-<ts>/`.

```sh
make spotify-import   URL=https://open.spotify.com/playlist/<id>
make spotify-rematch  PLAYLIST_ID=<id>

# live write (after review):
doppler run -p construct -c dev_af -- \
    python -m apps.spotify import https://open.spotify.com/playlist/<id> \
        --live --i-understand-the-risks --max-tracks 3
# prompt: type the playlist id to confirm.
```

Every live write takes a SQLite backup + emits a standalone reversal
script next to it.

## Matching (CONTEXT D2)

* ISRC exact (0.35) -- one-signal auto-match.
* Title (0.20) -- casefold + strip version suffix (remaster / radio edit).
* Artist set overlap (0.15) -- on normalised names.
* Duration (0.20) -- +/- 1500 ms.

Auto-match needs conf >= 0.70 AND >= 3 signals, OR a bare ISRC.
Conf >= 0.50 AND >= 2 signals -> review. Else -> unmatched.
