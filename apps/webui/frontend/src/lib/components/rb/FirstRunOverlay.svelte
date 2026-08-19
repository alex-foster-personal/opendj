<!--
	The first thing a new install says, over a dimmed library.

	It REPLACES an auto-goto('/setup'). A redirect navigated someone away from
	the app before they had seen it, which reads as "this thing does not work
	yet" rather than "there is one step left". Dimming instead leaves the
	library visible underneath: the ask arrives with its subject behind it.

	Deliberately not a <dialog>. This is not modal in the OS sense and it must
	not trap focus or swallow Escape -- someone who wants to look at the empty
	app first is allowed to, and the wizard is re-enterable from settings.

	Nothing here decides WHETHER to show; that is $lib/setup/first-run, which
	reads the daemon's verdict. Mounting this component is the whole contract.

	Painted from the app-level :root palette in app.css (--surface, --border,
	--fg, --muted, --accent), not the --rb-* set: those are scoped under
	.perf-root in rb/theme.css and would resolve to nothing on the library
	page this mounts over.
-->
<script lang="ts">
	import { goto } from '$app/navigation';

	const PRODUCT_NAME = 'Open DJ';
	const INVITATION = 'Import your library to get started';
</script>

<div class="first-run" role="presentation">
	<div class="card">
		<h2>{PRODUCT_NAME}</h2>
		<p>{INVITATION}</p>
		<button type="button" onclick={() => goto('/setup')}>Run setup</button>
	</div>
</div>

<style>
	.first-run {
		position: fixed;
		inset: 0;
		/* Translucent on purpose: the app has to stay visible beneath the ask. */
		background: rgba(0, 0, 0, 0.45);
		display: flex;
		align-items: center;
		justify-content: center;
		/* Above the table and its sticky header, below nothing else there is. */
		z-index: 100;
	}
	.card {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 0.75rem;
		padding: 1.75rem 2.25rem;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		color: var(--fg);
		text-align: center;
	}
	h2 {
		margin: 0;
		font-size: 1.25rem;
		letter-spacing: 0.02em;
	}
	p {
		margin: 0;
		color: var(--muted);
	}
	button {
		font: inherit;
		padding: 0.4rem 1.1rem;
		background: var(--accent);
		border: 1px solid var(--accent);
		border-radius: 4px;
		color: var(--bg);
		cursor: pointer;
	}
	button:hover {
		text-decoration: underline;
	}
</style>
