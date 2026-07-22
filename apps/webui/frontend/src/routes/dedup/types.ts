/**
 * Types for the /dedup review UI.
 *
 * Mirrors GET/POST /api/v1/dedup/clusters (duplicate-review-merge).
 * Cluster identity is independent from the display-only SQLite cluster id.
 * Every decision write is bound to cluster_key and the response revision.
 */

export type DecisionAction = 'merge' | 'keep-all' | 'skip';

export interface ClusterMember {
	stable_id: string;
	path: string;
	is_canonical: boolean;
	similarity: number | null;
	title: string | null;
	artist: string | null;
	bpm: number | null;
	key: string | null;
	duration_ms: number | null;
	rating: number | null;
	file_exists: boolean;
}

export interface Decision {
	cluster_key: string;
	survivor: string;
	action: DecisionAction;
	decided_at: string;
}

export interface Cluster {
	cluster_id: number;
	cluster_key: string;
	survivor_stable_id: string;
	rationale: string | null;
	flagged_manual_review: boolean;
	members: ClusterMember[];
	decision: Decision | null;
}

export interface ClustersResponse {
	clusters: Cluster[];
	note: string | null;
	revision: string;
}

export interface DecisionRecord {
	cluster_id: number;
	cluster_key: string;
	survivor: string;
	action: DecisionAction;
	decided_at: string;
	pending_apply: boolean;
	revision: string;
}
