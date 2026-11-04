/*
 * odj_live.h: C ABI of odj-live-core (apps/live-core), feature `ffi`.
 *
 * Build the static library with
 *   cargo rustc --release --features ffi --lib --crate-type staticlib
 * in apps/live-core; it lands at target/release/libodj_live_core.a.
 *
 * Lifecycle: odj_live_new -> odj_live_feed / odj_live_next_frame (repeat)
 * -> odj_live_finish -> odj_live_free. Every char* returned is a
 * NUL-terminated JSON object owned by the caller; release it with
 * odj_live_string_free. The frame and summary fields are documented in
 * apps/live-core/src/lib.rs (Frame, Summary).
 *
 * Memory is bounded. The summary keeps only the last 30 minutes of the
 * stream (STREAM_HISTORY_S in lib.rs); past that, its window_start_s says
 * where the summarized window starts and duration_s is its length. A frame
 * waits at most 60 s of stream time (MAX_PENDING_S) for odj_live_next_frame;
 * older frames are dropped and counted by odj_live_dropped_frames, so drain
 * after every feed.
 *
 * Not real-time safe: call from a worker thread fed by a ring buffer, never
 * from the audio render callback. One handle is not thread-safe; use it
 * from one thread at a time.
 */
#ifndef ODJ_LIVE_H
#define ODJ_LIVE_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct OdjLive OdjLive;

/* A new analyzer for mono input at sample_rate Hz (8000..384000), emitting
 * hz frames per second (above 0, at most 50). NULL when out of range. */
OdjLive *odj_live_new(uint32_t sample_rate, double hz);

/* Feed n mono samples. Returns the number of frames waiting, or -1 for a
 * NULL handle or samples pointer, a finished analyzer, or a non-finite
 * sample (the block is then dropped whole). */
int64_t odj_live_feed(OdjLive *handle, const float *samples, size_t n);

/* The oldest waiting frame as JSON. NULL means no frame is waiting or the
 * frame failed; odj_live_frame_error tells them apart. */
char *odj_live_next_frame(OdjLive *handle);

/* Why the last odj_live_next_frame returned NULL for a failure, as a JSON
 * object {"error": "<message>"}, or NULL for a frame, an empty queue or a
 * NULL handle. Release with odj_live_string_free. */
char *odj_live_frame_error(const OdjLive *handle);

/* Frames dropped because they waited more than 60 s of stream time undrained.
 * 0 for a NULL handle. */
uint64_t odj_live_dropped_frames(const OdjLive *handle);

/* End of stream: the summary as JSON. NULL means either the handle was
 * already finished (no error recorded) or the finish failed; odj_live_last_error
 * tells them apart. Frames made by the flush stay available to
 * odj_live_next_frame. */
char *odj_live_finish(OdjLive *handle);

/* Why the last odj_live_finish returned NULL, as a JSON object
 * {"error": "<message>"}, or NULL if it finished cleanly or has not finished.
 * Release with odj_live_string_free. */
char *odj_live_last_error(const OdjLive *handle);

/* Release a string returned above. NULL is ignored. */
void odj_live_string_free(char *s);

/* Release the analyzer. NULL is ignored. */
void odj_live_free(OdjLive *handle);

#ifdef __cplusplus
}
#endif

#endif /* ODJ_LIVE_H */
