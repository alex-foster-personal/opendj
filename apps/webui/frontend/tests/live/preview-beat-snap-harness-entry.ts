/** Live PVSNAP harness bundle entry: the planner, the grid reader, and the
 * deck's own Signalsmith schedule builder and node options, so a stretched
 * voice in the harness is configured exactly as a deck's is. */
export { planPreviewSnap, PREVIEW_SNAP_LEAD_SEC } from '../../src/lib/player/preview-beat-snap';
export { gridBeatPosition } from '../../src/lib/rb/beat-sync-math';
export { buildStretchSchedule, STRETCH_NODE_OPTIONS } from '../../src/lib/rb/stretch-adapter';
