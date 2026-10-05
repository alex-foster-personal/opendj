/**
 * Backward sync-seek crossfade. Lives outside audio-engine.svelte.ts so that
 * file stays within the file-size ratchet. The engine still owns scheduling.
 */
import {
	beatFourLeadInSec,
	syncSeekBlendDurationSec,
	type BeatGridBeat
} from '$lib/rb/sync-seek-blend';
import { AlignedStemDeckProcessor } from '$lib/rb/stem-graph';

interface BlendProcessor {
	disconnect(): void;
	connect(node: AudioNode): void;
}

interface BlendNodes {
	analyser: AudioNode;
}

interface BlendRuntime {
	audioBuffer: AudioBuffer | null;
	nodes: BlendNodes | null;
	processor: BlendProcessor | null;
	loadToken: unknown;
}

interface BlendState {
	anlz?: { beatgrid: { beats: readonly BeatGridBeat[] } } | null;
}

export async function scheduleFollowerBackwardBlend(
	deps: {
		context: AudioContext | null;
		state: BlendState;
		runtime: BlendRuntime;
		scheduleSync: (when: number, inputSec: number) => Promise<number>;
	},
	args: {
		syncAt: number;
		landingSec: number;
		tempoRatio: number;
		masterTempoEnabled: boolean;
		currentSec: number;
	}
): Promise<number> {
	const { syncAt, landingSec, tempoRatio, masterTempoEnabled, currentSec } = args;
	const { context: ctx, state: st, runtime: rt, scheduleSync } = deps;
	const buffer = rt.audioBuffer;
	const nodes = rt.nodes;
	const processor = rt.processor;
	if (
		ctx === null ||
		buffer === null ||
		nodes === null ||
		processor === null ||
		processor instanceof AlignedStemDeckProcessor ||
		masterTempoEnabled ||
		landingSec >= currentSec - 0.08
	) {
		return scheduleSync(syncAt, landingSec);
	}

	const beats = st.anlz?.beatgrid.beats ?? null;
	const beat4 = beats !== null && beats.length >= 2 ? beatFourLeadInSec(beats, landingSec) : null;
	const incomingSec =
		beat4 !== null && beat4 < landingSec - 0.05 && landingSec - beat4 <= 2.2 ? beat4 : landingSec;
	const blendDur = syncSeekBlendDurationSec(incomingSec, landingSec, tempoRatio);
	const t0 = Math.max(ctx.currentTime + 0.02, syncAt - blendDur);
	const tEnd = t0 + blendDur;

	processor.disconnect();
	const mainGain = ctx.createGain();
	const outGain = ctx.createGain();
	processor.connect(mainGain);
	mainGain.connect(nodes.analyser);
	outGain.connect(nodes.analyser);

	const outSrc = ctx.createBufferSource();
	outSrc.buffer = buffer;
	outSrc.playbackRate.value = tempoRatio;
	outSrc.connect(outGain);

	mainGain.gain.setValueAtTime(0.0001, t0);
	mainGain.gain.linearRampToValueAtTime(1, tEnd);
	outGain.gain.setValueAtTime(1, t0);
	outGain.gain.linearRampToValueAtTime(0.0001, tEnd);

	const startOffset = Math.min(Math.max(0, currentSec), Math.max(0, buffer.duration - 0.01));
	try {
		outSrc.start(t0, startOffset);
	} catch {
		try {
			mainGain.disconnect();
			outGain.disconnect();
		} catch {
			/* ignore */
		}
		try {
			processor.connect(nodes.analyser);
		} catch {
			/* ignore */
		}
		return scheduleSync(syncAt, landingSec);
	}

	const scheduled = await scheduleSync(t0, incomingSec);
	const token = rt.loadToken;
	const delayMs = Math.max(0, (tEnd - ctx.currentTime) * 1000) + 50;
	window.setTimeout(() => {
		try {
			outSrc.stop();
		} catch {
			/* already ended */
		}
		try {
			outSrc.disconnect();
			outGain.disconnect();
			mainGain.disconnect();
		} catch {
			/* ignore */
		}
		if (rt.loadToken !== token || rt.processor !== processor || rt.nodes === null) return;
		try {
			processor.disconnect();
			processor.connect(rt.nodes.analyser);
		} catch {
			/* ignore */
		}
	}, delayMs);

	return scheduled;
}
