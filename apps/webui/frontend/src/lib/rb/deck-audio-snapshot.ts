import type { DeckAudioSnapshot } from './deck-state-types';

export type BuildDeckAudioSnapshotInput = {
	renderContextTimeS: number;
	presentationContextTimeS: number;
	sampleRateHz: number;
	fftSize: number;
	frequencyDb: ArrayLike<number>;
	timeDomain: ArrayLike<number>;
};

function _validateClockFields(renderContextTimeS: number, presentationContextTimeS: number): void {
	if (!Number.isFinite(renderContextTimeS) || renderContextTimeS < 0) {
		throw new Error('deck audio snapshot requires finite render_context_time_s >= 0');
	}
	if (!Number.isFinite(presentationContextTimeS) || presentationContextTimeS < 0) {
		throw new Error('deck audio snapshot requires finite presentation_context_time_s >= 0');
	}
	if (presentationContextTimeS > renderContextTimeS) {
		throw new Error(
			'deck audio snapshot requires presentation_context_time_s <= render_context_time_s'
		);
	}
}

function _validateMetadata(sampleRateHz: number, fftSize: number): void {
	if (!Number.isFinite(sampleRateHz) || sampleRateHz <= 0) {
		throw new Error('deck audio snapshot requires sample_rate_hz > 0');
	}
	if (!Number.isFinite(fftSize) || !Number.isInteger(fftSize) || fftSize <= 0) {
		throw new Error('deck audio snapshot requires a positive integer fft_size');
	}
}

export function buildDeckAudioSnapshot(input: BuildDeckAudioSnapshotInput): DeckAudioSnapshot {
	const {
		renderContextTimeS,
		presentationContextTimeS,
		sampleRateHz,
		fftSize,
		frequencyDb,
		timeDomain
	} = input;

	_validateClockFields(renderContextTimeS, presentationContextTimeS);
	_validateMetadata(sampleRateHz, fftSize);

	const frequency_db = Array.from(frequencyDb);
	const time_domain = Array.from(timeDomain);

	if (time_domain.some((value) => !Number.isFinite(value))) {
		throw new Error('deck audio snapshot time_domain contains non-finite samples');
	}
	if (frequency_db.some((value) => !Number.isFinite(value))) {
		throw new Error('deck audio snapshot frequency_db contains non-finite samples');
	}

	return {
		render_context_time_s: renderContextTimeS,
		presentation_context_time_s: presentationContextTimeS,
		sample_rate_hz: sampleRateHz,
		fft_size: fftSize,
		frequency_db,
		time_domain
	};
}

export function copyDeckAudioSnapshot(snapshot: unknown): DeckAudioSnapshot {
	if (snapshot === null || typeof snapshot !== 'object') {
		throw new Error(
			'deck audio capture requires render_context_time_s and presentation_context_time_s'
		);
	}

	const record = snapshot as Record<string, unknown>;
	const renderContextTimeS = record.render_context_time_s;
	const presentationContextTimeS = record.presentation_context_time_s;

	if (!Number.isFinite(renderContextTimeS) || !Number.isFinite(presentationContextTimeS)) {
		throw new Error(
			'deck audio capture requires render_context_time_s and presentation_context_time_s'
		);
	}

	const sampleRateHz = record.sample_rate_hz;
	const fftSize = record.fft_size;
	const frequencyDb = record.frequency_db;
	const timeDomain = record.time_domain;

	if (!Array.isArray(frequencyDb) || !Array.isArray(timeDomain)) {
		throw new Error('deck audio capture has invalid frequency_db or time_domain arrays');
	}

	return buildDeckAudioSnapshot({
		renderContextTimeS: renderContextTimeS as number,
		presentationContextTimeS: presentationContextTimeS as number,
		sampleRateHz: sampleRateHz as number,
		fftSize: fftSize as number,
		frequencyDb: [...frequencyDb],
		timeDomain: [...timeDomain]
	});
}

export type DeckPcmEstimateInput = { audioBuffer: AudioBuffer | null; stemsReady: boolean };

/** Estimated decoded PCM retained for memory tracking.
 * Mix buffer always; when stems are ready, add 4 aligned part buffers
 * (AlignedStemDeckProcessor keeps vocals/drums/bass/other at the same geometry). */
export function estimateDeckPcmBytes(decks: Iterable<DeckPcmEstimateInput>): number {
	let total = 0;
	for (const deck of decks) {
		const buffer = deck.audioBuffer;
		if (buffer === null) continue;
		const mixBytes = buffer.length * buffer.numberOfChannels * 4;
		total += mixBytes;
		if (deck.stemsReady) total += mixBytes * 4;
	}
	return total;
}
