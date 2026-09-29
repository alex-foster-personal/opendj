/** TrackTable's truthful CloudSync/audio-location view. The backend publishes
 * durable cloud presence separately from local availability and ephemeral
 * transfer bytes, so none of the three requested states has to be guessed. */

export type TrackCloudKind =
	| 'not-on-cloud'
	| 'on-cloud-not-local'
	| 'on-cloud-and-local'
	| 'streaming';

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
	folderPath?: string | null;
	/** Real bytes from the backend's in-process transfer ledger. */
	transfer: TrackCloudTransferInput | null;
}

export function streamingProviderFromPath(folderPath: string | null | undefined): StreamingProvider {
	if (folderPath === null || folderPath === undefined || folderPath === '') return 'unknown';
	const lower = folderPath.toLowerCase();
	if (lower.startsWith('spotify:')) return 'spotify';
	if (lower.startsWith('tidal:')) return 'tidal';
	if (lower.startsWith('soundcloud:')) return 'soundcloud';
	return 'unknown';
}

function transferView(input: TrackCloudTransferInput | null): TrackCloudTransferView | null {
	if (input === null) return null;
	const action = input.direction === 'upload' ? 'Uploading to CloudSync' : 'Downloading from CloudSync';
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
	overlay: CloudTickOverlay
): TrackCloudView {
	return {
		kind,
		title: transfer === null ? title : `${title} ${transfer.label}`,
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
	if (input.isStreaming || input.spotifyPending) {
		return withTransfer(
			'streaming',
			'Streaming-service track; it is not part of CloudSync audio storage.',
			transfer,
			input.spotifyPending ? 'spotify' : streamingProviderFromPath(input.folderPath),
			'none'
		);
	}

	if (input.hasRemoteCopy && input.fileExists) {
		return withTransfer(
			'on-cloud-and-local',
			'On CloudSync and stored locally on this machine.',
			transfer,
			null,
			overlayForKind('on-cloud-and-local', input.fileExists)
		);
	}

	if (input.hasRemoteCopy) {
		return withTransfer(
			'on-cloud-not-local',
			'On CloudSync but not stored locally on this machine.',
			transfer,
			null,
			'none'
		);
	}

	return withTransfer(
		'not-on-cloud',
		input.fileExists
			? 'Not on CloudSync; audio is available only on this machine.'
			: 'Not on CloudSync and missing on this machine (broken link).',
		transfer,
		null,
		overlayForKind('not-on-cloud', input.fileExists)
	);
}
