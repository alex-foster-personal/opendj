// The inputs a capture measured: every per-track asset the page loaded (audio,
// ANLZ analysis, artwork, stems), plus the audio and analysis of each fixture
// id. Fixture tracks may resolve to library files, Rekordbox analysis or
// artwork outside the fixture data dir (track_locations, FolderPath,
// AnalysisDataPath, ImagePath, the online artwork cache), which the
// manifest's fixture digest never sees, so replacing one would change the work
// while the cohort looked unchanged. The driver records which asset URLs its
// pages requested; this fetches each before the measured phases (after an
// unmeasured warm-up has requested them all) and again once they are over,
// never alongside one, and hashes what the stack serves, status included.
import crypto from 'node:crypto';

// A per-track asset route of the app (track-source.ts trackApiPath): the
// bytes or analysis a deck load reads, not volatile library state.
const ASSET = /^\/api\/v1\/(usb\/)?tracks\/[^/]+\/(audio|anlz|artwork|stems\/[^/]+(\/waveform)?)$/;

export function isTrackAsset(pathname) {
	return ASSET.test(pathname) && !pathname.endsWith('/stems/state');
}

// Every fixture id's audio and /anlz (default points) are required to serve:
// a capture whose inputs cannot be fetched has no identity.
export function requiredAssets(ids) {
	return [...new Set(ids)].flatMap((id) => {
		const base = `${id.startsWith('usb-') ? '/api/v1/usb/tracks/' : '/api/v1/tracks/'}${encodeURIComponent(id)}`;
		return [`${base}/audio`, `${base}/anlz`];
	});
}

// { digests: {path?query: sha256 of "status\n" + body (2xx only)}, errors:
// [why] }. A required asset that does not answer 2xx, or any fetch that fails, is an
// error and gets no entry, never a missing key read as "unchanged"; an
// observed asset's non-2xx answer (no artwork) is its identity.
export async function mediaDigests(baseUrl, ids, observed = []) {
	const required = new Set(requiredAssets(ids));
	const digests = {};
	const errors = [];
	for (const ref of [...new Set([...required, ...observed])].sort()) {
		try {
			const res = await fetch(`${baseUrl}${ref}`);
			if (required.has(ref) && !res.ok) throw new Error(`HTTP ${res.status}`);
			const h = crypto.createHash('sha256').update(`${res.status}\n`);
			// a non-2xx answer's identity is its status: its body is a diagnostic
			// that can name stack-local paths (the data copy, the file it looked
			// for), which would make two stacks serving the same input differ
			if (res.ok && res.body) for await (const chunk of res.body) h.update(chunk);
			else await res.body?.cancel();
			digests[ref] = h.digest('hex');
		} catch (e) {
			errors.push(`cannot hash ${ref}: ${e?.message ?? e}`);
		}
	}
	return { digests, errors };
}

// The assets whose digest differs between two mediaDigests of one stack: an
// input replaced between them. An asset in only one of the two has no earlier
// identity to differ from, so it is not a change.
export function mediaChanges(before, after) {
	return Object.keys(after).filter((ref) => ref in before && before[ref] !== after[ref]).sort();
}

// Why the two sides of an A/B did not start from the same inputs: an asset
// hashed on one side only, or one whose digest differs between them. Each
// side is otherwise compared only with its own later digests, so a base and
// a fix that resolve one stable id to different audio, ANLZ, artwork or
// stems from the outset would never change during the run and would pass.
export function mediaSidesDiffer(a, b) {
	const out = [];
	for (const ref of [...new Set([...Object.keys(a), ...Object.keys(b)])].sort()) {
		if (!(ref in b)) out.push(`${ref} was hashed on A only`);
		else if (!(ref in a)) out.push(`${ref} was hashed on B only`);
		else if (a[ref] !== b[ref]) out.push(`${ref} differs between A and B`);
	}
	return out;
}
