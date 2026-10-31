/**
 * The remote speaker build flag on its own, so the Audio I/O panel can ask
 * whether to show the speaker entry without pulling the WebRTC and SDP helpers
 * of remote-speaker-core.ts into the /performance bundle.
 */

/**
 * The build flag. `vite.config.ts` sets it in ONE place: `on` for the dev
 * server, `off` for every built app, and `MUSIC_DJ_REMOTE_SPEAKER=on|off`
 * overrides either. A harness config that never sets it reads `off`: the
 * mode that opens no LAN listener is the only safe meaning for "nobody said".
 */
export type RemoteSpeakerFlag = 'on' | 'off';

export function remoteSpeakerFlag(configured: unknown): RemoteSpeakerFlag {
	if (configured === undefined) return 'off';
	if (configured !== 'on' && configured !== 'off') {
		throw new TypeError(`VITE_REMOTE_SPEAKER must be on or off, got ${String(configured)}`);
	}
	return configured;
}

/**
 * This build's flag. `import.meta.env` is absent where no bundler defined it
 * (a node test harness rendering the panel), and that reads `off` too. The
 * member expression is spelled out whole so vite's `define` replaces it.
 */
export function remoteSpeakerBuildFlag(): RemoteSpeakerFlag {
	return remoteSpeakerFlag(import.meta.env === undefined ? undefined : import.meta.env.VITE_REMOTE_SPEAKER);
}
