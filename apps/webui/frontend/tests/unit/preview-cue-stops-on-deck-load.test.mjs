// requirement: PVCUE-01
// A library strip audition in the headphones stops the moment a deck load is
// accepted (the maintainer's preview pin 3f7259934680), and keeps its decoded PCM so
// engine.load can reuse it.
// These pin WHERE the silence sits. The runtime proof, real decoded audio
// measured silent on the live cue bus, is tests/e2e/preview-cue-stops-on-deck-load.spec.ts.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const read = (path) => readFileSync(new URL(`../../src/${path}`, import.meta.url), 'utf8');

function executeBody() {
	const ipc = read('lib/rb/performance-ipc.svelte.ts');
	const start = ipc.indexOf('async function _execute(');
	assert.notEqual(start, -1, '_execute not found');
	return ipc.slice(start, ipc.indexOf('\n}\n', start));
}

test('an accepted deck load silences the preview before any engine runs it', () => {
	// if the silence comes after the engine dispatch (or never), the audition plays over the loaded deck -- broken.
	const body = executeBody();
	const silence = body.indexOf('silencePreviewCue()');
	assert.notEqual(silence, -1, 'a load must silence the preview');
	const guard = body.lastIndexOf("command.type === 'load'", silence);
	assert.ok(guard !== -1 && silence - guard < 200, 'the silence is gated on a load command');
	assert.ok(silence < body.indexOf('executeInRustEngine('), 'silence precedes the Rust engine dispatch');
});

test('a refused master load is not a load, so the audition keeps going (control)', () => {
	// if every load command silences before the refusal, a refused load onto the playing
	// master cuts the DJ's audition -- broken. The refusal throws first, under either engine.
	const body = executeBody();
	const silence = body.indexOf('silencePreviewCue()');
	const refusal = body.indexOf("(command.type === 'load' || command.type === 'unload') &&");
	assert.ok(refusal !== -1 && refusal < silence, 'the master refusal precedes the silence');
	assert.match(body.slice(refusal, silence), /refuseIfMaster === true &&\s*getDeckState\(command\.deck\)\.is_master\s*\) \{\s*throw new Error/);
});

test('a load the Web Audio engine refuses up front throws before the silence (Codex P2 4287743996)', () => {
	// if the silence comes first, a load onto a playing or audible deck is refused by
	// engine.load AFTER the audition was cut, so the refusal costs the DJ the audition - broken.
	const body = executeBody();
	const silence = body.indexOf('silencePreviewCue()');
	const guard = body.indexOf('assertDeckLoadAllowed(command.deck)');
	assert.ok(guard !== -1 && guard < silence, 'the engine load refusal precedes the silence');
	assert.match(body.slice(body.lastIndexOf("command.type === 'load'", guard), guard), /if \(!rustMode\.enabled\) $/);
	const engine = read('lib/rb/audio-engine.svelte.ts');
	const fn = engine.slice(engine.indexOf('export function assertDeckLoadAllowed'), engine.indexOf('function _assertCurrentDeckReplacementAllowed'));
	assert.match(fn, /_assertCurrentDeckReplacementAllowed\(deck\)/, 'it is the same check engine.load starts with');
});

test('silencing keeps the decoded buffer; stopping releases it', () => {
	// if silence released the buffer, every deck load from an audition would decode the file twice -- broken.
	const cue = read('lib/player/preview-cue.svelte.ts');
	const silenceFn = cue.slice(cue.indexOf('export function silencePreviewCue'), cue.indexOf('function _silencePreview'));
	assert.match(silenceFn, /_silencePreview\(false\)/);
	const stopFn = cue.slice(cue.indexOf('export function stopPreviewCue'), cue.indexOf('export function silencePreviewCue'));
	assert.match(stopFn, /_silencePreview\(true\)/);
	const inner = cue.slice(cue.indexOf('function _silencePreview'), cue.indexOf('\n}\n', cue.indexOf('function _silencePreview')));
	assert.match(inner, /if \(releaseBuffers\) _releaseBuffers\(\)/);
});

test('a preview request claims its generation before its first await', () => {
	// if the claim comes after ensureAudioGraphForCue(), a load accepted during a cold
	// graph start is overtaken and the audition starts over the loaded deck -- broken.
	// Runtime proof of the race: the third test in tests/e2e/preview-cue-stops-on-deck-load.spec.ts.
	const cue = read('lib/player/preview-cue.svelte.ts');
	const start = cue.indexOf('export async function previewCueSeek(');
	assert.notEqual(start, -1, 'previewCueSeek not found');
	const body = cue.slice(start, cue.indexOf('\n}\n', start));
	const claim = body.indexOf('const generation = ++_generation;');
	const firstAwait = body.indexOf('await ');
	assert.ok(claim !== -1 && claim < firstAwait, 'the generation claim precedes the first await');
	assert.equal([...body.matchAll(/\+\+_generation/g)].length, 1, 'one claim per request');
	const afterGraph = body.slice(body.indexOf('await ensureAudioGraphForCue()'), body.indexOf('previewCueReadiness()'));
	assert.match(afterGraph, /if \(generation !== _generation\) return \{ ok: false, reason: PREVIEW_SUPERSEDED \}/);
});
