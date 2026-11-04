<script lang="ts">
	/**
	 * /admin -> Spotify tab. Three panes, in the order an operator needs them:
	 *
	 * 1. CONNECTION. What the daemon can PROVE about the Spotify link. The
	 *    green state requires a completed live profile call; a client id and a
	 *    token file on disk render as amber "not verified", because a token can
	 *    be expired, revoked, or minted before the listen-history scope existed.
	 * 2. LISTEN HISTORY (the upsell). Spotify's last 50 plays, each scored
	 *    against the local library by the SAME matcher the playlist importer
	 *    uses. A play we do not own carries the five store links the
	 *    acquisition queue already uses. Buying is a browser hand-off on
	 *    purpose: agentic purchasing is not wired, so the button opens the
	 *    store rather than pretending to transact.
	 * 3. ACQUISITION QUEUE. Where the persisted, imported version of the same
	 *    idea lives, linked rather than duplicated.
	 *
	 * Every control here has an HTTP twin an agent can drive:
	 *   GET /api/v1/spotify/status
	 *   GET /api/v1/spotify/recently-played?limit=N
	 * and a CLI twin: `python -m apps.spotify recent [--unowned-only] [--json]`.
	 */
	import {
		PURCHASE_SOURCE_LABELS,
		SPOTIFY_PURCHASE_SOURCES,
		SPOTIFY_AUTH_COMMAND,
		fetchRecentPlays,
		fetchSpotifyStatus,
		type SpotifyRecentPlay,
		type SpotifyStatus
	} from './spotify-api';

	let status = $state<SpotifyStatus | null>(null);
	let statusError = $state<string | null>(null);
	let statusLoading = $state(false);

	let plays = $state<SpotifyRecentPlay[] | null>(null);
	let playsError = $state<string | null>(null);
	let playsLoading = $state(false);
	let unownedOnly = $state(false);
	let copied = $state(false);

	async function copyAuthCommand(): Promise<void> {
		try {
			await navigator.clipboard.writeText(SPOTIFY_AUTH_COMMAND);
			copied = true;
			setTimeout(() => (copied = false), 2000);
		} catch {
			// A refused clipboard is not a failure worth a banner: the command is
			// on screen in full and can be selected by hand.
			copied = false;
		}
	}

	const shown = $derived(
		plays === null ? [] : unownedOnly ? plays.filter((play) => play.ownership !== 'owned') : plays
	);
	const unownedCount = $derived(
		plays === null ? 0 : plays.filter((play) => play.ownership !== 'owned').length
	);

	async function loadStatus(): Promise<void> {
		if (statusLoading) return;
		statusLoading = true;
		statusError = null;
		try {
			status = await fetchSpotifyStatus();
		} catch (exc) {
			status = null;
			statusError = exc instanceof Error ? exc.message : String(exc);
		} finally {
			statusLoading = false;
		}
	}

	async function loadPlays(): Promise<void> {
		if (playsLoading) return;
		playsLoading = true;
		playsError = null;
		try {
			plays = await fetchRecentPlays(50);
		} catch (exc) {
			plays = null;
			playsError = exc instanceof Error ? exc.message : String(exc);
		} finally {
			playsLoading = false;
		}
	}

	/** Formats a Spotify ISO-8601 played_at in the viewer's own timezone. */
	function playedAt(raw: string): string {
		const when = new Date(raw);
		if (Number.isNaN(when.getTime())) return raw;
		return when.toLocaleString(undefined, {
			month: 'short',
			day: 'numeric',
			hour: '2-digit',
			minute: '2-digit'
		});
	}

	function duration(ms: number | null): string {
		if (ms === null || ms <= 0) return '-';
		const total = Math.round(ms / 1000);
		return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
	}

	$effect(() => {
		if (status === null && statusError === null && !statusLoading) void loadStatus();
	});
</script>

<section class="panel">
	<h3>Connection</h3>
	<p class="sub">
		Served by <code>GET /api/v1/spotify/status</code>. "Connected" is only shown after the daemon
		completed a live profile call against your account - a client id and a token file on disk are
		reported separately, because neither proves the token still works.
	</p>

	{#if statusError !== null}
		<div class="fatal">STATUS PROBE FAILED{'\n\n'}{statusError}</div>
	{:else if status === null}
		<p class="note">Probing Spotify...</p>
	{:else}
		<div class="status-row">
			<span
				class="pill"
				class:ok={status.connected}
				class:bad={!status.connected}
				title={status.connected
					? 'A live /me call returned this profile just now.'
					: 'No live profile call succeeded. The reason is printed below.'}
			>
				{status.connected ? 'Connected' : 'Not connected'}
			</span>
			{#if status.connected}
				<span class="who">
					{status.display_name ?? status.account_id ?? 'unknown account'}
					{#if status.product !== null}<span class="tier" title="Spotify account tier as the Web API reports it. Soloist and the Web Playback SDK both require Premium.">{status.product}</span>{/if}
				</span>
			{/if}
		</div>

		<dl class="facts">
			<dt title="Whether the daemon process itself has SPOTIFY_CLIENT_ID in its environment. Start it under doppler run to supply it.">SPOTIFY_CLIENT_ID</dt>
			<dd class:bad-text={!status.client_id_present}>
				{status.client_id_present ? 'present in the daemon environment' : 'MISSING'}
			</dd>
			<dt title="The PKCE token cache written by the one-time browser sign-in. Its presence is not proof the token still works.">Token cache</dt>
			<dd class:bad-text={!status.token_cache_present}>
				{status.token_cache_present ? 'present' : 'absent'}
				<code>{status.token_cache_path}</code>
			</dd>
			<dt title="The OAuth scopes this build requests. Adding a scope invalidates the cached token and re-runs the browser sign-in once.">Scopes</dt>
			<dd><code>{status.scopes.join(' ')}</code></dd>
		</dl>

		{#if status.needs_authorization}
			<div class="authorize">
				<p class="authorize-head">Spotify needs authorizing on this machine.</p>
				<p class="authorize-body">
					There is deliberately no sign-in button here. The OAuth flow (PKCE) opens a browser and
					waits on a <code>127.0.0.1</code> callback, so it only completes where a person is
					sitting - a daemon running it would open a browser nobody can see and hang the request.
					Run this once in a terminal and the panes below come alive:
				</p>
				<div class="cmd-row">
					<code class="cmd">{SPOTIFY_AUTH_COMMAND}</code>
					<button class="act" onclick={() => void copyAuthCommand()}>
						{copied ? 'Copied' : 'Copy'}
					</button>
				</div>
				<p class="authorize-note">
					Expect this after any scope change: a token cached under a narrower scope set counts as
					no token at all, which is exactly what listen history triggered.
				</p>
			</div>
		{/if}

		{#if status.error !== null && !status.needs_authorization}
			<div class="fatal">{status.error}</div>
		{/if}
		<button class="act" onclick={() => void loadStatus()} disabled={statusLoading}>
			{statusLoading ? 'Probing...' : 'Re-probe'}
		</button>
	{/if}
</section>

<section class="panel">
	<h3>Listen history</h3>
	<p class="sub">
		Spotify's last 50 plays for this account, newest first, served by
		<code>GET /api/v1/spotify/recently-played</code>. Each play is scored against your local library
		by the same matcher the playlist importer uses, so a row marked <strong>owned</strong> would have
		matched on import too. Nothing here is persisted: a play is not an acquisition-queue entry until
		you import the playlist it came from.
	</p>
	<p class="note">
		<strong>Buying opens a browser tab.</strong> Agentic purchasing is not wired, so a buy button hands
		off to the store's search page rather than transacting. The five stores are the same set, in the
		same order, as the acquisition queue's persisted links.
	</p>

	<div class="controls">
		<button class="act" onclick={() => void loadPlays()} disabled={playsLoading}>
			{playsLoading ? 'Fetching...' : plays === null ? 'Fetch listen history' : 'Refresh'}
		</button>
		<label class="toggle" title="Hide plays the local library already has, leaving only the buy candidates.">
			<input type="checkbox" bind:checked={unownedOnly} disabled={plays === null} />
			Not in my library only
		</label>
		{#if plays !== null}
			<span class="tally" title="Plays returned by Spotify, and how many of them the local library does not have.">
				{plays.length} plays, {unownedCount} not owned
			</span>
		{/if}
	</div>

	{#if playsError !== null}
		<div class="fatal">LISTEN HISTORY FAILED{'\n\n'}{playsError}</div>
	{:else if plays === null}
		<p class="note">Not fetched yet. This pane never renders without a live call behind it.</p>
	{:else if plays.length === 0}
		<p class="note">Spotify returned no plays for this account in its recent-plays window.</p>
	{:else if shown.length === 0}
		<p class="note">Every recent play is already in the local library.</p>
	{:else}
		<table class="plays">
			<thead>
				<tr>
					<th>Played</th>
					<th>Track</th>
					<th title="Track length as Spotify reports it, mm:ss.">Length</th>
					<th title="owned = auto-matched to a local track. review = a likely match below the auto-match threshold, so check before buying. not owned = no candidate passed the matcher.">In library</th>
					<th>Buy</th>
				</tr>
			</thead>
			<tbody>
				{#each shown as play (play.played_at + play.spotify_uri)}
					<tr>
						<td class="when" title={play.played_at}>{playedAt(play.played_at)}</td>
						<td class="track">
							<span class="title">{play.title}</span>
							<span class="artist">{play.artist}{play.album !== null ? ` - ${play.album}` : ''}</span>
						</td>
						<td class="num" title={play.duration_ms === null ? 'Spotify returned no duration.' : `${play.duration_ms} ms as Spotify reports it.`}>
							{duration(play.duration_ms)}
						</td>
						<td>
							<span
								class="own own-{play.ownership}"
								title={play.ownership === 'owned'
									? `Auto-matched at confidence ${play.confidence} to local track ${play.matched_stable_id}.`
									: play.ownership === 'review'
										? `Best candidate scored ${play.confidence}, below the auto-match threshold of 0.70. Check before buying.`
										: `No local candidate scored above the review threshold (best ${play.confidence}).`}
							>
								{play.ownership === 'owned'
									? 'owned'
									: play.ownership === 'review'
										? 'review'
										: 'not owned'}
							</span>
						</td>
						<td class="buy">
							{#if play.ownership === 'owned'}
								<span class="note-inline">-</span>
							{:else}
								{#each SPOTIFY_PURCHASE_SOURCES as source (source)}
									<a
										class="store"
										href={play.suggested_sources[source]}
										target="_blank"
										rel="noopener noreferrer"
										title={`Opens ${PURCHASE_SOURCE_LABELS[source]}'s search for this track in a new tab. No purchase is made here.`}
									>
										{PURCHASE_SOURCE_LABELS[source]}
									</a>
								{/each}
							{/if}
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{/if}
</section>

<section class="panel">
	<h3>Imported playlists and acquisition queue</h3>
	<p class="sub">
		The persisted half of the same idea. Imported Spotify playlists and their pending-purchase rows
		live in the library browser's Spotify source, not here, so there is exactly one place that
		renders them.
	</p>
	<a class="act link" href="/performance?source=spotify">Open the Spotify source in the browser</a>
	<p class="note">
		Import a playlist with
		<code>python -m apps.spotify import &lt;url&gt;</code> (dry run first), then
		<code>--live --i-understand-the-risks</code> to write.
	</p>
</section>

<style>
	.panel {
		border: 1px solid var(--border, #2a2a2a);
		border-radius: 8px;
		padding: 1rem 1.1rem;
		margin-bottom: 1rem;
	}
	h3 {
		margin: 0 0 0.35rem;
		font-size: 1rem;
	}
	.sub,
	.note {
		margin: 0 0 0.6rem;
		font-size: 0.82rem;
		line-height: 1.45;
		opacity: 0.8;
	}
	.note {
		opacity: 0.62;
	}
	.fatal {
		white-space: pre-wrap;
		border: 1px solid #a33;
		background: rgba(170, 51, 51, 0.12);
		border-radius: 6px;
		padding: 0.6rem 0.7rem;
		font-size: 0.82rem;
		margin: 0.5rem 0;
	}
	.authorize {
		border: 1px solid #d0a54a;
		background: rgba(208, 165, 74, 0.1);
		border-radius: 6px;
		padding: 0.7rem 0.8rem;
		margin: 0.5rem 0;
	}
	.authorize-head {
		margin: 0 0 0.4rem;
		font-size: 0.86rem;
		font-weight: 600;
	}
	.authorize-body,
	.authorize-note {
		margin: 0 0 0.5rem;
		font-size: 0.8rem;
		line-height: 1.45;
		opacity: 0.85;
	}
	.authorize-note {
		margin-bottom: 0;
		opacity: 0.62;
	}
	.cmd-row {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		flex-wrap: wrap;
		margin-bottom: 0.5rem;
	}
	.cmd {
		flex: 1 1 20rem;
		font-size: 0.76rem;
		padding: 0.35rem 0.5rem;
		border-radius: 4px;
		background: rgba(127, 127, 127, 0.16);
		user-select: all;
		word-break: break-all;
	}

	.status-row {
		display: flex;
		align-items: center;
		gap: 0.6rem;
		margin-bottom: 0.6rem;
	}
	.pill {
		border-radius: 999px;
		padding: 0.15rem 0.6rem;
		font-size: 0.78rem;
		border: 1px solid currentColor;
	}
	.pill.ok {
		color: #4caf7d;
	}
	.pill.bad {
		color: #d08a4a;
	}
	.who {
		font-size: 0.85rem;
	}
	.tier {
		margin-left: 0.4rem;
		opacity: 0.65;
		font-size: 0.78rem;
	}
	.facts {
		display: grid;
		grid-template-columns: max-content 1fr;
		gap: 0.25rem 0.9rem;
		margin: 0 0 0.6rem;
		font-size: 0.82rem;
	}
	.facts dt {
		opacity: 0.65;
	}
	.facts dd {
		margin: 0;
	}
	.bad-text {
		color: #d08a4a;
	}
	code {
		font-size: 0.78rem;
		opacity: 0.85;
	}
	.controls {
		display: flex;
		align-items: center;
		gap: 0.8rem;
		flex-wrap: wrap;
		margin-bottom: 0.6rem;
	}
	.toggle {
		font-size: 0.82rem;
		display: flex;
		align-items: center;
		gap: 0.35rem;
	}
	.tally {
		font-size: 0.82rem;
		opacity: 0.7;
	}
	.act {
		font: inherit;
		font-size: 0.82rem;
		padding: 0.3rem 0.7rem;
		border-radius: 6px;
		border: 1px solid var(--border, #2a2a2a);
		background: transparent;
		color: inherit;
		cursor: pointer;
	}
	.act:hover:not(:disabled) {
		background: rgba(127, 127, 127, 0.12);
	}
	.act:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.act.link {
		display: inline-block;
		text-decoration: none;
	}
	table.plays {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.82rem;
	}
	.plays th {
		text-align: left;
		font-weight: 500;
		opacity: 0.6;
		border-bottom: 1px solid var(--border, #2a2a2a);
		padding: 0.3rem 0.5rem;
	}
	.plays td {
		padding: 0.35rem 0.5rem;
		border-bottom: 1px solid rgba(127, 127, 127, 0.14);
		vertical-align: top;
	}
	.when {
		white-space: nowrap;
		opacity: 0.7;
	}
	.num {
		text-align: right;
		white-space: nowrap;
	}
	.track .title {
		display: block;
	}
	.track .artist {
		display: block;
		opacity: 0.62;
	}
	.own {
		font-size: 0.75rem;
		border-radius: 4px;
		padding: 0.1rem 0.4rem;
		border: 1px solid currentColor;
		white-space: nowrap;
	}
	.own-owned {
		color: #4caf7d;
	}
	.own-review {
		color: #d0b04a;
	}
	.own-unmatched {
		color: #d08a4a;
	}
	.buy {
		display: flex;
		flex-wrap: wrap;
		gap: 0.3rem;
	}
	.store {
		font-size: 0.75rem;
		border-radius: 4px;
		padding: 0.1rem 0.4rem;
		border: 1px solid var(--border, #2a2a2a);
		text-decoration: none;
		color: inherit;
		opacity: 0.85;
	}
	.store:hover {
		background: rgba(127, 127, 127, 0.14);
		opacity: 1;
	}
	.note-inline {
		opacity: 0.4;
	}
</style>
