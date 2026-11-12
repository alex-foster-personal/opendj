/**
 * File name and Location column text derived from `file_path` (issue #10030).
 */
import {
	isStreamingAvailability,
	serviceDisplayName,
	type StreamingRowFacts,
	uriScheme
} from '$lib/components/rb/browser/streaming-availability';

export interface FilePathCellRow extends StreamingRowFacts {
	file_path?: string | null | undefined;
}

export interface FilePathCellText {
	fileName: string;
	location: string;
}

/** Split a local path into parent folder and final segment (posix or Windows). */
export function splitLocalFilePath(filePath: string): FilePathCellText {
	if (filePath.length === 0) return { fileName: '', location: '' };
	const win = filePath.includes('\\');
	const parts = filePath.split(/[/\\]/).filter((part) => part.length > 0);
	if (parts.length === 0) return { fileName: '', location: '' };
	const fileName = parts[parts.length - 1] ?? '';
	const dirParts = parts.slice(0, -1);
	if (dirParts.length === 0) return { fileName, location: '' };
	const location = win ? dirParts.join('\\') : `/${dirParts.join('/')}`;
	return { fileName, location };
}

function streamingProviderLabel(row: FilePathCellRow): string {
	const scheme =
		(row.streaming_provider && row.streaming_provider !== 'unknown'
			? row.streaming_provider
			: null) ??
		uriScheme(row.file_path) ??
		uriScheme(row.rb_meta?.folder_path);
	if (scheme === null || scheme === 'unknown') return 'Streaming';
	return serviceDisplayName(scheme);
}

/** Visible File name and Location cells for one library row. */
export function filePathCellsFor(row: FilePathCellRow): FilePathCellText {
	if (isStreamingAvailability(row)) {
		return { fileName: '', location: streamingProviderLabel(row) };
	}
	const path = row.file_path ?? '';
	return splitLocalFilePath(path);
}

/** Case-insensitive sort key for File name (null last via sortRows). */
export function fileNameSortValue(row: FilePathCellRow): string | null {
	const { fileName } = filePathCellsFor(row);
	if (fileName.length === 0) return null;
	return fileName.toLocaleLowerCase();
}

/** Case-insensitive sort key for Location. */
export function locationSortValue(row: FilePathCellRow): string | null {
	const { location } = filePathCellsFor(row);
	if (location.length === 0) return null;
	return location.toLocaleLowerCase();
}
