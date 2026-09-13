/** TrackTable's truthful CloudSync/audio-location view. The backend publishes
 * durable cloud presence separately from local availability and ephemeral
 * transfer bytes, so none of the three requested states has to be guessed. */

export type TrackCloudKind =
	| 'not-on-cloud'
	| 'on-cloud-not-local'
	| 'on-cloud-and-local'
	| 'streaming';

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
}

export interface TrackCloudInput {
	fileExists: boolean;
	isStreaming: boolean;
	hasRemoteCopy: boolean;
	/** Real bytes from the backend's in-process transfer ledger. */
	transfer: TrackCloudTransferInput | null;
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
	transfer: TrackCloudTransferView | null
): TrackCloudView {
	return {
		kind,
		title: transfer === null ? title : `${title} ${transfer.label}`,
		showIcon: true,
		transfer
	};
}

export function trackCloudView(input: TrackCloudInput): TrackCloudView {
	const transfer = transferView(input.transfer);
	if (input.isStreaming) {
		return withTransfer(
			'streaming',
			'Streaming-service track; it is not part of CloudSync audio storage.',
			transfer
		);
	}

	if (input.hasRemoteCopy && input.fileExists) {
		return withTransfer(
			'on-cloud-and-local',
			'On CloudSync and stored locally on this machine.',
			transfer
		);
	}

	if (input.hasRemoteCopy) {
		return withTransfer(
			'on-cloud-not-local',
			'On CloudSync but not stored locally on this machine.',
			transfer
		);
	}

	return withTransfer(
		'not-on-cloud',
		input.fileExists
			? 'Not on CloudSync; audio is available only on this machine.'
			: 'Not on CloudSync and missing on this machine (broken link).',
		transfer
	);
}
