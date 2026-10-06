/**
 * Shared-graph entry for stem-work-signal.test.mjs: the REAL stem download
 * and the REAL stem decode policy in one bundle, so the registry the download
 * writes is the exact module instance the test reads.
 */
export { fetchStemAudioArrayBuffers, listPlaylistTracksPage } from '$lib/rb/api-rb';
export { blockStemDecode, inFlightStemWorkCount } from '$lib/rb/stem-decode-policy';
