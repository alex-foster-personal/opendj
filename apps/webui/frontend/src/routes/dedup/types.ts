/**
 * Types for the /dedup review UI.
 *
 * Mirrors GET/POST /api/v1/dedup/clusters (duplicate-review-merge).
 * Everything for this route is co-located here: this dir must NOT import
 * from $lib/api.ts or $lib/rb/* (those are owned by concurrent workflows).
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
	survivor: string;
	action: DecisionAction;
	decided_at: string;
}

export interface Cluster {
	cluster_id: number;
	survivor_stable_id: string;
	rationale: string | null;
	flagged_manual_review: boolean;
	members: ClusterMember[];
	decision: Decision | null;
}

export interface ClustersResponse {
	clusters: Cluster[];
	note: string | null;
}

export interface DecisionRecord {
	cluster_id: number;
	survivor: string;
	action: DecisionAction;
	decided_at: string;
	pending_apply: boolean;
}
