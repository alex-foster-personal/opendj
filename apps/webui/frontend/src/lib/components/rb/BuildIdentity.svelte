<!--
	WHAT AM I -- the build stamp, on the main surface, in every route.

	This is deliberately NOT in an About panel. The failure it exists to
	prevent is somebody using a stale or dirty build for an hour without
	noticing, and a readout you have to go looking for cannot prevent that.
	It is fixed to the corner of the window, one line, always present, on the
	performance route as well as the app shell -- so it is mounted at the
	layout root rather than inside the shell chrome.

	Everything shown is read at RUNTIME: the engine half over HTTP, the shell
	half from the global the Tauri binary injected. There is no build-time
	string literal in this bundle and no fallback value anywhere. When a side
	cannot state what it is, this renders a visible fault, because "unknown"
	and "fine" must never look the same.

	The whole component is one self-contained file (plus its logic module,
	$lib/rb/build-identity.ts, which the unit tests drive). The root layout
	gains exactly one import and one mount line.
-->
<script lang="ts">
	import { onMount } from 'svelte';

	import {
		describeDrift,
		explainSide,
		fetchEngineBuild,
		formatStamp,
		readShellBuild,
		shortLabel,
		type EngineStamp,
		type ShellStamp,
		type SideState
	} from '$lib/rb/build-identity';

	// The shell stamp is synchronous: it was injected before any script ran.
	const shell: SideState<ShellStamp> = readShellBuild();
	let engine = $state<SideState<EngineStamp>>({ kind: 'loading' });
	let expanded = $state(false);

	const drift = $derived(describeDrift(shell, engine));
	const engineStamp = $derived(engine.kind === 'ok' ? formatStamp(engine.value.built_at_utc) : null);
	const shellStamp = $derived(shell.kind === 'ok' ? formatStamp(shell.value.built_at_utc) : null);

	/** The lane this artifact was built as, or the fact that it has none. */
	const lane = $derived(
		engine.kind === 'ok' ? (engine.value.lane_label ?? 'unlabelled') : '?'
	);

	const driftTitle = $derived(
		drift === 'drifted'
			? 'DRIFT: the desktop shell and the engine were built from different commits. One of them is stale.'
			: drift === 'aligned'
				? 'Shell and engine were built from the same commit.'
				: 'Shell and engine cannot be compared: at least one of them did not state a commit.'
	);

	onMount(() => {
		// One read, at mount. The identity of a running process does not
		// change, so polling it would be a lie dressed as freshness.
		void fetchEngineBuild().then((result) => {
			engine = result;
		});
	});
</script>

<div
	class="build-identity"
	class:fault={engine.kind === 'fault' || shell.kind === 'fault'}
	class:drifted={drift === 'drifted'}
>
	<button
		type="button"
		class="summary"
		title={`This build's identity, read at runtime -- never a value compiled into this page.\n\nENGINE (serves this page): ${explainSide('engine', engine)}\n\nSHELL (the desktop window): ${explainSide('shell', shell)}\n\n${driftTitle}\n\nClick for the full stamp.`}
		aria-expanded={expanded}
		onclick={() => (expanded = !expanded)}
	>
		<span class="lane" title="The bake-off lane this artifact was built as, from the payload manifest.">
			{lane}
		</span>
		<span class="sha" title={explainSide('engine', engine)}>{shortLabel(engine)}</span>
		{#if engine.kind === 'ok' && engine.value.git_dirty}
			<span
				class="dirty"
				title="DIRTY: uncommitted changes were present in the working tree when this engine was built, so the commit above does not fully describe what is running."
			>
				DIRTY
			</span>
		{/if}
		{#if drift === 'drifted'}
			<span class="drift" title={driftTitle}>SHELL DRIFT</span>
		{/if}
		{#if engineStamp !== null}
			<span
				class="when"
				title={`Built ${engineStamp.local} local time, ${engineStamp.utc} UTC. Source: ${engine.kind === 'ok' && engine.value.built_at_kind === 'payload-build' ? 'when the payload was packaged' : "the HEAD commit's timestamp, because this engine runs from a source checkout"}.`}
			>
				{engineStamp.local}
			</span>
		{/if}
	</button>

	{#if expanded}
		<dl class="detail">
			<dt title="The daemon serving this page.">engine</dt>
			<dd>
				{#if engine.kind === 'ok'}
					<code>{engine.value.git_sha_full}</code>
					<span class="meta">
						{engine.value.git_branch} · {engine.value.source} · v{engine.value.engine_version}
						{#if engine.value.git_dirty}<b>· DIRTY</b>{/if}
					</span>
					{#if engineStamp !== null}
						<span class="meta">{engineStamp.local} · {engineStamp.utc} UTC</span>
					{/if}
				{:else}
					<span class="reason">{engine.kind === 'loading' ? 'reading...' : engine.reason}</span>
				{/if}
			</dd>

			<dt title="The native desktop window, if this page is running inside one.">shell</dt>
			<dd>
				{#if shell.kind === 'ok'}
					<code>{shell.value.git_sha_full}</code>
					<span class="meta">
						{shell.value.git_branch} · v{shell.value.app_version}
						{#if shell.value.git_dirty}<b>· DIRTY</b>{/if}
					</span>
					{#if shellStamp !== null}
						<span class="meta">{shellStamp.local} · {shellStamp.utc} UTC</span>
					{/if}
				{:else}
					<span class="reason">{shell.kind === 'loading' ? 'reading...' : shell.reason}</span>
				{/if}
			</dd>
		</dl>
	{/if}
</div>

<style>
	.build-identity {
		position: fixed;
		left: 0.35rem;
		bottom: 0.35rem;
		z-index: 60;
		max-width: min(38rem, calc(100vw - 1rem));
		font-size: 0.68rem;
		line-height: 1.35;
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		color: var(--muted, #8a8a8a);
		background: color-mix(in srgb, var(--bg, #1b1b1b) 88%, transparent);
		border: 1px solid color-mix(in srgb, currentColor 25%, transparent);
		border-radius: 3px;
		pointer-events: auto;
	}
	.summary {
		display: flex;
		gap: 0.4rem;
		align-items: baseline;
		padding: 0.1rem 0.4rem;
		border: none;
		background: transparent;
		color: inherit;
		font: inherit;
		cursor: help;
	}
	.lane {
		font-weight: 700;
		color: var(--accent, #d97757);
	}
	.sha {
		letter-spacing: 0.02em;
	}
	.when {
		opacity: 0.72;
	}
	/* A dirty or drifted build must not be able to pass for a clean one at a
	   glance, so these are the only two colours in the readout. */
	.dirty,
	.drift {
		font-weight: 700;
		color: #e0b341;
	}
	.build-identity.drifted,
	.build-identity.fault {
		border-color: #e0b341;
	}
	.detail {
		display: grid;
		grid-template-columns: auto 1fr;
		gap: 0.1rem 0.5rem;
		margin: 0;
		padding: 0.25rem 0.4rem 0.35rem;
		border-top: 1px solid color-mix(in srgb, currentColor 20%, transparent);
	}
	.detail dt {
		font-weight: 700;
	}
	.detail dd {
		margin: 0;
		display: flex;
		flex-direction: column;
	}
	.detail code {
		word-break: break-all;
	}
	.meta {
		opacity: 0.72;
	}
	.reason {
		color: #e0b341;
	}
</style>
