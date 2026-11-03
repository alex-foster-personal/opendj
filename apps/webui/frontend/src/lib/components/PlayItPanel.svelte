<script lang="ts">
	import { onMount } from 'svelte';
	import {
		getPlaylist,
		PlaylistConflictError,
		PlayItError,
		replacePlaylistTracks,
		solvePlayIt,
		type PlayItSolveOut,
		type PlaylistDetail
	} from '$lib/api';
	import { publishSetGoalPins } from '$lib/rb/autoplay-queue.svelte';
	import PlayItPanelView from './PlayItPanelView.svelte';

	let { playlistId }: { playlistId: string } = $props();

	let durationMin = $state(60);
	let peakAtMin = $state<number | null>(null);
	let playlistTracks = $state<PlaylistDetail['tracks']>([]);
	let peakPins = $state<string[]>([]);
	let openerPins = $state<string[]>([]);
	let closerPin = $state<string>('');

	let solving = $state(false);
	let solveError = $state<string | null>(null);
	let result = $state<PlayItSolveOut | null>(null);

	let applying = $state(false);
	let applyError = $state<string | null>(null);
	let applied = $state<{ previousOrder: string[]; undoEtag: string } | null>(null);

	onMount(async () => {
		const detail = await getPlaylist(playlistId);
		playlistTracks = detail.tracks;
	});

	function publishPinsFromResult(solve: PlayItSolveOut): void {
		const roleOf = new Map<string, 'opener' | 'peak' | 'closer'>();
		for (const step of solve.steps) {
			if (step.pin_role) {
				roleOf.set(step.stable_id, step.pin_role);
			}
		}
		publishSetGoalPins(roleOf);
	}

	async function solve(): Promise<void> {
		solving = true;
		solveError = null;
		applied = null;
		try {
			result = await solvePlayIt(playlistId, {
				duration_min: durationMin,
				peak_at_min: peakAtMin,
				peak_pins: peakPins,
				opener_pins: openerPins,
				closer_pin: closerPin || null
			});
			publishPinsFromResult(result);
		} catch (err) {
			result = null;
			solveError = err instanceof PlayItError ? err.message : `${err}`;
		} finally {
			solving = false;
		}
	}

	async function apply(): Promise<void> {
		if (!result) return;
		applying = true;
		applyError = null;
		try {
			const { etag } = await replacePlaylistTracks(playlistId, result.proposed_order, result.etag);
			applied = { previousOrder: result.previous_order, undoEtag: etag };
		} catch (err) {
			if (err instanceof PlaylistConflictError) {
				applyError = 'Playlist changed elsewhere since you solved -- re-run PLAY IT and try again.';
				result = null;
			} else {
				applyError = `${err}`;
			}
		} finally {
			applying = false;
		}
	}

	async function undo(): Promise<void> {
		if (!applied) return;
		applying = true;
		applyError = null;
		try {
			await replacePlaylistTracks(playlistId, applied.previousOrder, applied.undoEtag);
			applied = null;
			result = null;
		} catch (err) {
			if (err instanceof PlaylistConflictError) {
				applyError = 'Playlist changed elsewhere since you applied -- cannot undo automatically.';
			} else {
				applyError = `${err}`;
			}
		} finally {
			applying = false;
		}
	}
</script>

<PlayItPanelView
	bind:durationMin
	bind:peakAtMin
	bind:peakPins
	bind:openerPins
	bind:closerPin
	{playlistTracks}
	{solving}
	{solveError}
	{result}
	{applying}
	{applyError}
	{applied}
	{solve}
	{apply}
	{undo}
/>
