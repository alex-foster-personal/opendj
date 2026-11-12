<!--
	/lighting: the stage visualiser (issue #9898) with the overrides panel (issue #9899)
	mounted below it.
-->
<script lang="ts">
	import { onDestroy, untrack } from 'svelte';

	import LightingOverrides from '$lib/components/lighting/LightingOverrides.svelte';

	import { connectFrames, fetchPatch, type FramesStatus } from '$lib/lighting/frames-client';
	import { createLightingStageIpc, installLightingStageIpc } from '$lib/lighting/stage-ipc';
	import {
		activePatch,
		applyStageCommand,
		currentFrame,
		newStageSession,
		stageQuery,
		type StageCommand,
		type StageSession
	} from '$lib/lighting/stage-session';
	import {
		renderStage,
		stageSignature,
		type DrawCommand,
		type FrameMessage,
		type StagePatch,
		type StageSize
	} from '$lib/lighting/stage-render';

	let canvasEl: HTMLCanvasElement | undefined = $state();
	let containerEl: HTMLDivElement | undefined = $state();

	let session = $state<StageSession>(newStageSession());
	let mode = $derived(session.mode);
	let playing = $derived(session.playing);
	let elapsedS = $derived(session.elapsed_s);
	let durationS = $derived(session.replay?.durationS ?? 0);

	let statusLine = $state('Connecting…');
	let statusRole: 'status' | 'alert' = $state('status');
	let signature = $state('');

	let patch: StagePatch | null = $state(null);
	let liveFrame: FrameMessage | null = $state(null);

	let closeSocket: (() => void) | null = null;
	let patchInflight = false;
	let lastRequestedPatchVersion: string | number | null = null;
	let liveFrameSeen = false;
	let rafId: number | null = null;
	let lastRafMs = 0;
	let resizeObs: ResizeObserver | null = null;
	let stageSize: StageSize = $state({ width: 800, height: 450, margin: 32 });

	function setStatus(line: string, role: 'status' | 'alert' = 'status'): void {
		statusLine = line;
		statusRole = role;
	}

	function ownerQuery() {
		return stageQuery(session, { frame: liveFrame, patch });
	}

	function dispatch(cmd: StageCommand) {
		const prev = session;
		let next: StageSession;
		try {
			next = applyStageCommand(prev, cmd);
		} catch (err) {
			const msg = err instanceof Error ? err.message : String(err);
			session = { ...prev, last_error: msg };
			setStatus(msg, 'alert');
			throw err;
		}
		session = next;

		if (prev.mode === 'live' && next.mode === 'replay') {
			closeSocket?.();
			closeSocket = null;
		}
		if (cmd.type === 'load_replay' && next.mode === 'replay' && next.replay !== null) {
			setStatus(
				`Replay: ${next.replay.frames.length} frames, ${next.replay.durationS.toFixed(2)}s`
			);
			applyStageLayout();
		}
		if (prev.mode === 'replay' && next.mode === 'live') {
			stopRaf();
			connectLive();
		}
		if (next.mode === 'replay' && (cmd.type === 'load_replay' || next.playing !== prev.playing)) {
			startReplayLoop();
		}

		return ownerQuery();
	}

	function draw(ctx: CanvasRenderingContext2D, commands: DrawCommand[]): void {
		for (const cmd of commands) {
			switch (cmd.kind) {
				case 'clear':
					ctx.fillStyle = cmd.color;
					ctx.fillRect(0, 0, cmd.width, cmd.height);
					break;
				case 'beam':
					ctx.strokeStyle = cmd.color;
					ctx.lineWidth = cmd.lineWidth;
					ctx.beginPath();
					ctx.moveTo(cmd.x1, cmd.y1);
					ctx.lineTo(cmd.x2, cmd.y2);
					ctx.stroke();
					break;
				case 'disc':
					ctx.beginPath();
					ctx.arc(cmd.x, cmd.y, cmd.r, 0, Math.PI * 2);
					ctx.fillStyle = cmd.color;
					ctx.fill();
					ctx.strokeStyle = cmd.stroke;
					ctx.lineWidth = 1;
					ctx.stroke();
					break;
				case 'flash':
					ctx.save();
					ctx.globalAlpha = 0.9;
					ctx.fillStyle = cmd.color;
					ctx.beginPath();
					ctx.arc(cmd.x, cmd.y, cmd.r, 0, Math.PI * 2);
					ctx.fill();
					ctx.restore();
					break;
				case 'label':
					ctx.fillStyle = cmd.color;
					ctx.font = '11px sans-serif';
					ctx.textAlign = 'center';
					ctx.fillText(cmd.text, cmd.x, cmd.y);
					break;
				default: {
					const _exhaustive: never = cmd;
					throw new Error(`unknown draw kind: ${String(_exhaustive)}`);
				}
			}
		}
	}

	function paint(): void {
		const el = canvasEl;
		if (el === undefined) return;
		const ctx = el.getContext('2d');
		if (ctx === null) return;

		let frame: FrameMessage | null = null;
		const ap = activePatch(session, patch);
		const activeStagePatch = ap.patch;
		let timeS: number | undefined;

		if (mode === 'live') {
			frame = liveFrame;
		} else {
			const at = currentFrame(session);
			if (at !== null) {
				frame = at.frame;
				timeS = at.frame.t_ns / 1e9;
			}
		}

		const commands =
			timeS === undefined
				? renderStage(frame, activeStagePatch, stageSize)
				: renderStage(frame, activeStagePatch, stageSize, { timeS });
		signature = stageSignature(commands);
		draw(ctx, commands);
	}

	function applyStageLayout(): void {
		const box = containerEl;
		if (box === undefined) return;
		const rect = box.getBoundingClientRect();
		const cssW = Math.max(320, rect.width);
		const cssH = Math.round((cssW * 9) / 16);
		const dpr = typeof globalThis.devicePixelRatio === 'number' ? globalThis.devicePixelRatio : 1;
		stageSize = { width: cssW, height: cssH, margin: 32 };
		const el = canvasEl;
		if (el !== undefined) {
			el.width = cssW * dpr;
			el.height = cssH * dpr;
			el.style.width = `${cssW}px`;
			el.style.height = `${cssH}px`;
			const ctx = el.getContext('2d');
			if (ctx !== null) ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
		}
	}

	function connectLive(): void {
		lastRequestedPatchVersion = null;
		liveFrameSeen = false;
		setStatus('Connecting…');

		void (async () => {
			try {
				patch = await fetchPatch();
				setStatus('Live: patch loaded, waiting for frames');
			} catch (err) {
				patch = null;
				const msg = err instanceof Error ? err.message : String(err);
				if (msg.includes('no_patch')) {
					setStatus(
						'Live: no patch saved yet (opendj lighting patch); frames still draw in fallback layout'
					);
				} else {
					setStatus(`Live: no patch (${msg})`);
				}
			}
		})();

		closeSocket?.();
		closeSocket = connectFrames(
			(frame) => {
				liveFrame = frame;
				if (!liveFrameSeen) {
					liveFrameSeen = true;
					setStatus('Live: receiving frames');
				}
				const pv = frame.patch_version;
				if (
					pv !== null &&
					pv !== patch?.version &&
					pv !== lastRequestedPatchVersion &&
					!patchInflight
				) {
					lastRequestedPatchVersion = pv;
					patchInflight = true;
					void fetchPatch()
						.then((next) => {
							patch = next;
						})
						.catch(() => {
							/* keep prior patch */
						})
						.finally(() => {
							patchInflight = false;
						});
				}
			},
			(st: FramesStatus) => {
				if (st.state === 'live') return;
				if (st.state === 'connecting') {
					setStatus('Connecting to frames…');
					return;
				}
				if (st.state === 'closed' || st.state === 'error') {
					setStatus(`No frames from the daemon (${st.detail}). Load a frames file to replay.`);
				}
			}
		);
	}

	function tickReplay(now: number): void {
		if (!playing || session.replay === null) return;
		if (lastRafMs > 0) {
			const dt = (now - lastRafMs) / 1000;
			dispatch({ type: 'tick', dt_s: dt });
		}
		lastRafMs = now;
		if (session.playing) {
			rafId = requestAnimationFrame(tickReplay);
		}
	}

	function startReplayLoop(): void {
		stopRaf();
		lastRafMs = 0;
		if (playing) rafId = requestAnimationFrame(tickReplay);
	}

	function stopRaf(): void {
		if (rafId !== null) {
			cancelAnimationFrame(rafId);
			rafId = null;
		}
	}

	async function onReplayFile(ev: Event): Promise<void> {
		const input = ev.currentTarget as HTMLInputElement;
		const file = input.files?.[0];
		if (file === undefined) return;
		try {
			const text = await file.text();
			dispatch({ type: 'load_replay', text, name: file.name });
		} catch {
			/* dispatch already surfaced the error */
		}
		input.value = '';
	}

	function togglePlay(): void {
		dispatch({ type: 'toggle_play' });
	}

	function onScrub(ev: Event): void {
		const input = ev.currentTarget as HTMLInputElement;
		dispatch({ type: 'seek', elapsed_s: Number(input.value) });
	}

	function backToLive(): void {
		dispatch({ type: 'go_live' });
	}

	$effect(() => {
		const canvas = canvasEl;
		const container = containerEl;
		if (canvas === undefined || container === undefined) return undefined;

		untrack(() => {
			applyStageLayout();
			connectLive();
		});

		const disposeIpc = installLightingStageIpc(
			createLightingStageIpc({
				dispatch,
				query: ownerQuery,
				signature: () => signature
			})
		);

		if (typeof ResizeObserver !== 'undefined') {
			const obs = new ResizeObserver(() => {
				untrack(() => applyStageLayout());
			});
			obs.observe(container);
			resizeObs = obs;
		}

		return () => {
			disposeIpc();
			resizeObs?.disconnect();
			resizeObs = null;
			closeSocket?.();
			closeSocket = null;
			stopRaf();
		};
	});

	$effect(() => {
		session;
		liveFrame;
		patch;
		stageSize;
		const canvas = canvasEl;
		if (canvas === undefined) return;
		untrack(() => paint());
	});

	onDestroy(() => {
		closeSocket?.();
		stopRaf();
		resizeObs?.disconnect();
	});
</script>

<svelte:head><title>Lighting stage</title></svelte:head>

<div class="lighting-page">
	<header>
		<h1>Lighting stage</h1>
		<p role={statusRole}>{statusLine}</p>
	</header>

	<div class="stage-wrap" bind:this={containerEl}>
		<canvas bind:this={canvasEl} data-testid="lighting-stage" data-stage-signature={signature}
		></canvas>
	</div>

	<div class="controls">
		<label title="Load a frames JSON file for offline replay without the daemon websocket">
			Replay a frames file
			<input
				type="file"
				accept=".json,application/json"
				data-testid="lighting-replay-file"
				title="Choose a frames JSON replay file"
				onchange={onReplayFile}
			/>
		</label>

		{#if mode === 'replay' && session.replay !== null}
			<button type="button" title="Play or pause replay playback" onclick={togglePlay}>
				{playing ? 'Pause' : 'Play'}
			</button>
			<input
				type="range"
				min="0"
				max={durationS}
				step="0.01"
				value={elapsedS}
				aria-label="Replay position (seconds)"
				title="Scrub replay position in seconds"
				oninput={onScrub}
			/>
			<button
				type="button"
				title="Leave replay and reconnect to live frames websocket"
				onclick={backToLive}
			>
				Back to live
			</button>
		{/if}
	</div>

	<LightingOverrides />
</div>

<style>
	.lighting-page {
		min-height: 100vh;
		padding: 12px 16px;
		background: #0a0a0e;
		color: #c8c8d0;
		font-family: system-ui, sans-serif;
	}
	h1 {
		margin: 0 0 4px;
		font-size: 1.1rem;
		font-weight: 600;
	}
	header p {
		margin: 0 0 12px;
		font-size: 0.9rem;
	}
	.stage-wrap {
		width: 100%;
		max-width: 960px;
		aspect-ratio: 16 / 9;
	}
	canvas {
		display: block;
		width: 100%;
		height: 100%;
		border: 1px solid #33343c;
		border-radius: 4px;
	}
	.controls {
		margin-top: 12px;
		display: flex;
		flex-wrap: wrap;
		gap: 12px;
		align-items: center;
		font-size: 0.9rem;
	}
	.controls label {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}
	button {
		padding: 6px 12px;
		background: #2a2a34;
		border: 1px solid #444;
		color: inherit;
		border-radius: 4px;
		cursor: pointer;
	}
</style>
