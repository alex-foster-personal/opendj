/**
 * The Settings waveform preview's sample: a REAL rekordbox detail waveform, so
 * the thumbnail shows what each design x palette looks like on music (the maintainer,
 * Tue 6 Oct 2026: "can the example be a more realistic audio sample so user
 * gets a better idea what they're choosing").
 *
 * Source: seconds 8..20 of the 24 s deck-row window in
 * tests/unit/fixtures/blocks-variance-anlz-window.json (128 BPM, so about 25
 * beats, including a section change; provenance in its manifest), window
 * points 1201..3003, each band downsampled by per-bucket max over 2 points
 * (as the server downsamples detail; it keeps the library route's initial
 * load inside its gzip budget). 12 s rather than 24 s so a
 * card-wide preview shows single beats the way a deck row does. Stored as the
 * PWV byte values (0..127, value = byte / 127), base64.
 * Regenerate from that fixture, never hand-edit.
 */
import type { AnlzWaveform } from '$lib/rb/anlz-types';

const BANDS = {
	low: 'LCsoKSYuKjAvKiktKyglIyUiJigmIycnLCosKSYpJystKykmJCIgIiAfIR8eJyUkJSQjMy8tKignKCYnJiQlJCYmJSooLCkoJiYlJCQiIiAgJSMqKCgpJyUjISEmJCQkIiopKCYrKSktKzAuLC8vMC0vLjIwNDYyMjUxLSsqKScqJyooJi0qLC0qKigmKicnJSMiJCUjJS0tMjAtKjMyNDEwLTIxNjIwLCssKygpJyonJCwpJyQiICQiJCIhIyUjJCQnLCwtKywqJyYnKzYyLywqJy4tKykpKjMvLS0sLy0sKSopJiQjIB8uLSonKi0wLSs0My8sLy0sKicnJCIhHicmJCIgHiglIiAeHR8dHBofJSQiIiEiIh8dHCAfHRoaGRcXGRgXGBgYGBkeIR8dHBseHRsZIB8dGxoYFxcaGBcZGBcZGBcdIB4cGhkYFxgaHx4dGxoZFxYWFhcWFBQVFBMSEREQExMSEhESFhQTEhAQFBMTEhEQERERGxsZFxkYFxUUFxgXFxYWGhgWFBMSERAPDg4gHx0cGhkYFhUUExUTEhMVFBMREA8PERIQDw4XGxoZFxUUFBIVExIRFBIREBITERAPDg8PDhUUEhIQDw4NDBgXFhQTEhEQEhEQDw4NDg0MGRgWFRUUExEQDxcVExIREA8ODQ4NDAwMCwoNFRUUFBMVFBMSEBMRERAVExEQDw4NDAsLCgwVFRQUExIREhEUFRMSERAPDg0MDAwMDAwLCg8SERAQDw8REQ8SEhAPDg0MCwsNDQwLCgsKChYZGBYVExIREA8RExEREA4ODQwLDQwMDAsKGhkYFhUUExIREBUUEhITEhEQDw8ODQwLCgoRFhUYFxUUEhEQFxYUExIREA4ODg0NDQ4NDAsNDAsLCgsQDw4UFBMRExEQDw4ODg4TERAODRUVExIREBEQDhASERAPEBAPDg4NDQ0MCwoKCREQDxAPFBMSERAPDQwODQwLDw8NDAsLCgoJExMVFBMREhAQERAPDxMREREPEBMSEA8ODQwPDg0MDAsNDg0MEBQSEhEQDw4NDA8ODQwLCgsXFhQSExIRDxFEQj44NC4pJSE2NTEuKSUhHho+PDgzLyomIyI4NzMvKiYiHzY4NjItKSQhHTAzMi4qJiMfHDU0MSwpJSEeGzY0MSwoJCAdMUI+OTQvKiYiNDw5NC8qJiIfOTc0MA==',
	mid: 'Ki0pNTI1Nzg2Ozk8NDAuLywuPjo2NDYxMDQxLisvSUU/REM/ODIsKTY0LikmQDo7SEFITUhLRj89Qz1FPzlFPTg1OEY/O0I/ODI1Mzc1MTY1MDYxMjEvL0lFSENAODYwKyomLywwKjc6OzYxLSo2ST41MDUyMz42MC0rOzY/PDkyLyosKjEvPzgxLDQxNC8pJiQnKisxQTkyPDg0LjAtKyctLTIuNi8qLSkyLyklPkNAOTYwLSgjLCkjLC0qJyUqKCc0QT5EQklGQzw3MSwuPzY3MCwrJispKCgnJU5JSEFEPjo2MTY8NjAtNTIvPDg0Ljk/OzMxUk5HP0Q8NDcwPTUxLC85NDEsLywoLygkIB0sPDYyODMtKSU0MTIsKSQqJyQhIykqJiEhHh0sKiY1LikkISMrJyUhISQhJyIhIiAgHiAgIy04Ni8rJiMlICoqKCQlJSMgHSUnJCMfHiciKSciHx8oJCEfHCQgIh8hHh8eIyEnJSMeJCIlOTUwKjYyLSclMDArJSolJB8hJCAhHSAeGyFAPTgzNTItKCU4NTYvKSQgIR8dHBkoIx8cGhgoNTEsJycmJCMyMComJCAfICAmISgmIR4bGigmIiEeHBsZFy8sJiIhIh4eIh0gHR4jHxsdMS0pJSckJCIgKCYjISUqJyMfGxocGx0lIR0aOzYyLisxLCcjMy0oJCEoIyQgHhsgIB0dGiA9OzUwKiYjIh8kNS4oJCEeHygjIR8jIR0bHSQ1MSwpKCUjIiMzLCYhHh0aGRckIikkIBwaFyc3NS4qKickHyExMiorKyQjHx0cJyIeJCEdSUQ+ODQ2MSwnKjI1MSspJyIlKSUhJSAdGxgkPDYxMConIiAgNC4qNDArJiAiJyUhIiUiHhslIx8cJiYpJyInLSklLy8pJSEjIR8oIx4bGTU2MS8tMSwoIx4vKiUgKCUjISUmJSIeGxgWGCcmMC8rNDItKCooIx8jIR4iISwmIh4dHBkWJCo3MCsnJiMhHi4yLTIuLCkkIDYvLCcjHxseHSEdISAmKiQfMS0oLywoIx8dJiQiIh4hHRk0MConLSspIyI+OzcvKiUgHyo1NDEtJyMfHjBCP01KQzoyKzRUS0I6MywnIzU1MS8pJB8bOD45NTQvKiglPlVKQDcxKyYgKjMxLCgkIB0ZR0c/NzEsJSAbNzo4MCsnIh49O0Q9Nw==',
	high: 'CAkIBwYHBwYGERgVEA8NCwoNKDEjGRMOCw4KDgwLCwkHBwcXExASEAwSDAgNDg0KHhgQCwofIh4WEAwJCBETExAMDQ8NCxEPDAkICwoJCwsPDAoNCwkICDItJBsVEBAOCRsTHhkRDAkQCggLCQ0LBwYGGhYjGxQPFA0JIR4YEg4LCwsKDgwJCwkJBwYFEhMTERENDgsWEyAbERUYFxEcICQaEhMNCwwdGhIQExIQDg8PDQ8ODg0KCgsMEAwJBwkIBhYnISEdFQ8NCg4UFQ8NDhMQEg8LCwoJCg0MChsgGRkZFBoUKjk0LSwjHCQfHhccIhsbGBgWHhwYEhAOCw0LEA4LCggJBwgHCAcFCAUFBQQFDAkJBggHCAYLDAoHBQgGBQgFBQQFBwYEBQQLCgkJBgUEBQQJBwYFBAYFBAQDAwcGBQgFBA4MCQYEBQQDBAkHBgUJBwUFBAYFBAQFBAQDBg0KCAYLCQYFBgYGBwQEBgUEAwgHBgYEAwIDDQkGBQQFBAMDBgkHBQUEBAYFBQQHBgQEAwIFCggHBwYEAwMHDQgGBQgHBAMEAwMIBQQDAwIJBwQEBQUFBAQHDAkHBgYEBQQDAwUFAwMDAwkHBgUEBgQEAwULCQYGBQQDAwYFBAMEBAMCCQ0JBwQEBAUEBQgHBQMHBQQGAwcFBAMEBAMEDwoIBgQDBQUKCQYEAwIEAwMDAwQDAwICAwIFBQUEBgUDAwIJCAUFAwMDAgMDBQUEAwICAgsJBwUDBwUDAgsKBgQDBAMCAQMDBgQFBAMCAgoJCQYFBAIDAwgHBwUDAwIEAwIEAwIDAgIBCQsLCwcGBAMCEAsIBgQDAgICAgIEAwICAgQKCwoJBwYGBAIGBwYFBAMCAgECBAQEBAUDAgIODgkJBgQDAgELCgcGBwUDAgIDBAMCAgIDAg4MDgoIBgQDAg8MCAUFAwICAQIDAwICAgIBAQoNCwgJBgQDCxQRCwcGBAMCAQECAgMCAgEBERAOCgcEAwIBDQkJBwUFAwIBAgMEBAMCAQIMFA4KCAUEAgEWEgwIBgUDAgEDBQMDBAMCAQEPDgsIBwUEAiYwIxkUEw0JBUQ4JxwVEAsHBTAzJiYgFw8JBjIrIRoVEQoGDzsqHhYQDAgFOTEkHBUQCwgFNCwgGSIYDwkGQS8lIRwUDQgFOCknHRUPCgYfNCUcGRIMCAUuKiAZGA=='
} as const;

/** Points per band (901). */
export const PREVIEW_SAMPLE_POINTS = 901;

/** Seconds of music the sample spans. */
export const PREVIEW_SAMPLE_SECONDS = 12;

function _decode(b64: string): number[] {
	const raw = atob(b64);
	const out = new Array<number>(raw.length);
	for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i) / 127;
	return out;
}

export function previewSampleBands(): { low: number[]; mid: number[]; high: number[] } {
	return { low: _decode(BANDS.low), mid: _decode(BANDS.mid), high: _decode(BANDS.high) };
}

/** The preview's waveform object. Built once per page: the deck painter caches
 * its per-waveform norms by identity, so repaints reuse them. */
let _sample: AnlzWaveform | undefined;

export function previewSampleWaveform(): AnlzWaveform {
	if (_sample === undefined) {
		const detail = { length: PREVIEW_SAMPLE_POINTS, ...previewSampleBands() };
		_sample = { kind: 'tri', preview: detail, detail };
	}
	return _sample;
}
