/**
 * Autolist index worker (issue #2066). Thin onmessage wrapper around autolist-index.ts.
 */
import {
	buildAutolistIndex,
	genreBucketsFromIndex,
	queryAutolistIndex,
	staticBpmBuckets,
	staticRatingBuckets,
	type AutolistIndexRow
} from './autolist-index';
import type { AutolistSelection } from './autolist-rule';

let index: ReturnType<typeof buildAutolistIndex> | null = null;

self.onmessage = (event: MessageEvent) => {
	const msg = event.data as {
		type: string;
		rows?: AutolistIndexRow[];
		selection?: AutolistSelection;
		group?: string;
		seq?: number;
	};
	if (msg.type === 'build' && msg.rows !== undefined) {
		index = buildAutolistIndex(msg.rows);
		self.postMessage({ type: 'built', count: msg.rows.length });
		return;
	}
	if (msg.type === 'query' && msg.selection !== undefined) {
		if (index === null) {
			self.postMessage({ type: 'query-result', ids: [], seq: msg.seq });
			return;
		}
		const ids = queryAutolistIndex(index, msg.selection);
		self.postMessage({ type: 'query-result', ids, seq: msg.seq });
		return;
	}
	if (msg.type === 'buckets' && msg.group !== undefined) {
		if (msg.group === 'genre') {
			const buckets = index !== null ? genreBucketsFromIndex(index) : [];
			self.postMessage({ type: 'buckets', group: msg.group, buckets, seq: msg.seq });
			return;
		}
		if (msg.group === 'rating') {
			self.postMessage({
				type: 'buckets',
				group: msg.group,
				buckets: staticRatingBuckets(),
				seq: msg.seq
			});
			return;
		}
		if (msg.group === 'bpm') {
			self.postMessage({
				type: 'buckets',
				group: msg.group,
				buckets: staticBpmBuckets(),
				seq: msg.seq
			});
		}
	}
};
