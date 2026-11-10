# Mixxx-created library fixture

`mixxx-2.5.4-scanned.sqlite` was written by Mixxx itself, not by hand or by a test helper.

- Mixxx 2.5.4 (Ubuntu package `2.5.4+dfsg-1build1`), schema version 39, run on nucbox-wsl Mon 9 Nov 2026.
- Run 1: `mixxx --settingsPath <dir>` with `QT_QPA_PLATFORM=offscreen` created the schema.
- Run 2: with `/tmp/mixxx-fixture-music` in `directories` and `[Library] RescanOnStartup 1`,
  Mixxx scanned one 2 s 44.1 kHz stereo WAV (`tone.wav`) into `library` and `track_locations`.
- Every table, column, default and the scanned track row are Mixxx output. Tests copy the file and
  add cues, a playlist and a crate through that real schema; they never edit the committed file.
