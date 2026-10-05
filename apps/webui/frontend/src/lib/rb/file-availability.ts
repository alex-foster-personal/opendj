/**
 * A library row's file availability verdict, as the engine reports it.
 *
 * Its own module so a consumer that needs only this type does not import
 * all of api-rb.ts (frontend.max_fan_in ratchet); api-rb re-exports it.
 */
export type FileAvailabilityStatus =
	| 'present'
	| 'absent'
	| 'AVAILABILITY_PENDING'
	| 'streaming'
	| 'awaiting_volume'
	/** UI read of a stored `streaming` row, named from the URI scheme. */
	| `${string}-streaming`;
