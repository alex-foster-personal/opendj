/** TrackTable's truthful CloudSync/audio-location view. The backend publishes
 * durable cloud presence separately from local availability and ephemeral
 * transfer bytes, so none of the three requested states has to be guessed. */

import { streamingLoadRefusal } from './streaming-availability';

export type TrackCloudKind =
	| 'not-on-cloud'
	| 'on-cloud-not-local'
	| 'on-cloud-and-local'
	| 'streaming'
	/** OFFLOAD: in the cloud on purpose; a deck load downloads it first. */
	| 'offloaded'
	/** OFFLOAD: downloaded for 24 h; it goes back to the cloud after. */
	| 'temporary';

export type StreamingProvider = 'spotify' | 'tidal' | 'soundcloud' | 'unknown';

export type CloudTickOverlay = 'none' | 'green-tick' | 'blue-tick';

export interface TrackCloudTransferInput {
	direction: 'upload' | 'download';
	bytesTransferred: number;
	bytesTotal: number | null;
}

export interface TrackCloudTransferView {
	direction: 'upload' | 'download';
	percent: number | null;
	label: string;
}

export interface TrackCloudView {
	kind: TrackCloudKind;
	title: string;
	showIcon: boolean;
	transfer: TrackCloudTransferView | null;
	provider: StreamingProvider | null;
	overlay: CloudTickOverlay;
}

export interface TrackCloudInput {
	fileExists: boolean;
	isStreaming: boolean;
	hasRemoteCopy: boolean;
	/** Inline row fact (TrackRowOut.spotify_pending): an unmatched Spotify
	 * placeholder. It has no rekordbox mapping, so rb_meta and its
	 * folder_path never arrive; this flag alone names the provider. */
	spotifyPending: boolean;
	/** Inline row provider; wins over folderPath, which only rb_meta carries. */
	provider?: StreamingProvider | null | undefined;
	folderPath?: string | null;
	/** Location the listing probed (`file_path`). Shown on an unavailable row. */
	filePath?: string | null;
	fileAvailability?: string | null;
	/** Real bytes from the backend's in-process transfer ledger. */
	transfer: TrackCloudTransferInput | null;
	/** OFFLOAD: the row's offload state (null when never offloaded here). */
	offload?: { state: string; expires_at: string | null } | null | undefined;
}

function expiryText(expiresAt: string | null): string {
	if (expiresAt === null) return 'later today';
	const at = new Date(expiresAt);
	return Number.isNaN(at.getTime()) ? 'later today' : `at ${at.toLocaleString()}`;
}

/** Hover text for an unavailable cloud icon: the path probed, and why. */
export function unavailableIconTitle(input: {
	filePath?: string | null;
	fileAvailability?: string | null;
	fileExists: boolean;
}): string | null {
	let reason: string | null = null;
	if (input.fileAvailability === 'awaiting_volume') reason = 'awaiting volume';
	else if (input.fileAvailability === 'AVAILABILITY_PENDING') reason = 'not yet checked';
	else if (input.fileAvailability === 'absent' || input.fileExists === false) {
		reason = 'missing on this machine';
	}
	if (reason === null) return null;
	const path =
		typeof input.filePath === 'string' && input.filePath.length > 0
			? input.filePath
			: '(no file path)';
	return `${path} (${reason})`;
}

export function streamingProviderFromPath(
	folderPath: string | null | undefined
): StreamingProvider {
	if (folderPath === null || folderPath === undefined || folderPath === '') return 'unknown';
	const lower = folderPath.toLowerCase();
	if (lower.startsWith('spotify:')) return 'spotify';
	if (lower.startsWith('tidal:')) return 'tidal';
	if (lower.startsWith('soundcloud:')) return 'soundcloud';
	return 'unknown';
}

function transferView(input: TrackCloudTransferInput | null): TrackCloudTransferView | null {
	if (input === null) return null;
	const action =
		input.direction === 'upload' ? 'Uploading to CloudSync' : 'Downloading from CloudSync';
	if (input.bytesTotal === null) {
		return {
			direction: input.direction,
			percent: null,
			label: `${action}: ${input.bytesTransferred} bytes transferred; exact percentage unavailable.`
		};
	}
	const percent =
		input.bytesTotal === 0
			? 100
			: Math.min(100, Math.max(0, (input.bytesTransferred / input.bytesTotal) * 100));
	return {
		direction: input.direction,
		percent,
		label: `${action}: ${Math.round(percent)}% (${input.bytesTransferred} of ${input.bytesTotal} bytes).`
	};
}

function withTransfer(
	kind: TrackCloudKind,
	title: string,
	transfer: TrackCloudTransferView | null,
	provider: StreamingProvider | null,
	overlay: CloudTickOverlay,
	unavailable: string | null
): TrackCloudView {
	const base = transfer === null ? title : `${title} ${transfer.label}`;
	return {
		kind,
		title: unavailable === null ? base : `${base} ${unavailable}`,
		showIcon: true,
		transfer,
		provider,
		overlay
	};
}

function overlayForKind(kind: TrackCloudKind, fileExists: boolean): CloudTickOverlay {
	if (kind === 'on-cloud-and-local') return 'green-tick';
	if (kind === 'not-on-cloud' && fileExists) return 'blue-tick';
	return 'none';
}

export function trackCloudView(input: TrackCloudInput): TrackCloudView {
	const transfer = transferView(input.transfer);
	const offload = input.offload?.state;
	if (offload === 'offloaded' || offload === 'restoring') {
		const title =
			offload === 'restoring'
				? 'Downloading from the cloud.'
				: 'Offloaded to the cloud to save disk. Loading it onto a deck downloads it first.';
		return withTransfer('offloaded', title, transfer, null, 'none', null);
	}
	if (offload === 'temporary') {
		const title = `Downloaded for 24h; it goes back to the cloud ${expiryText(input.offload?.expires_at ?? null)}.`;
		return withTransfer('temporary', title, transfer, null, 'none', null);
	}
	const unavailable = unavailableIconTitle(input);
	if (input.isStreaming || input.spotifyPending) {
		const provider = input.spotifyPending
			? 'spotify'
			: (input.provider ?? streamingProviderFromPath(input.folderPath));
		const title = input.spotifyPending
			? 'Streaming-service track; it is not part of CloudSync audio storage.'
			: streamingLoadRefusal({
					file_availability: input.fileAvailability,
					is_streaming: input.isStreaming,
					file_path: input.filePath ?? input.folderPath,
					streaming_provider: provider
				});
		return withTransfer('streaming', title, transfer, provider, 'none', null);
	}

	if (input.hasRemoteCopy && input.fileExists) {
		return withTransfer(
			'on-cloud-and-local',
			'On CloudSync and stored locally on this machine.',
			transfer,
			null,
			overlayForKind('on-cloud-and-local', input.fileExists),
			unavailable
		);
	}

	if (input.hasRemoteCopy) {
		return withTransfer(
			'on-cloud-not-local',
			'On CloudSync but not stored locally on this machine.',
			transfer,
			null,
			'none',
			unavailable
		);
	}

	return withTransfer(
		'not-on-cloud',
		input.fileExists
			? 'Not on CloudSync; audio is available only on this machine.'
			: 'Not on CloudSync and missing on this machine (broken link).',
		transfer,
		null,
		overlayForKind('not-on-cloud', input.fileExists),
		unavailable
	);
}
