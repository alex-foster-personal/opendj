/**
 * Reactive state + API calls for the in-app review/feedback widget.
 *
 * Rune module - .svelte.ts is required for $state; only $state is used (no
 * $derived/$effect) so the node:test harness can bundle it, the same
 * constraint jobs-store.svelte.ts documents.
 *
 * AVAILABILITY IS PROBED, NEVER ASSUMED. The feedback routes shipped after
 * the engine payloads the maintainer already has installed, so this same SPA build can
 * face a daemon with no /api/v1/feedback at all. One hydrate probe decides:
 * 404 marks the surface 'missing' and the chevron renders inert (FB-01);
 * any other failure leaves 'unknown' so the next toggle retries - a daemon
 * that is still booting must not get branded feature-less forever.
 *
 * NO OPTIMISTIC WRITES. Every mutation talks to the daemon and re-renders
 * from the response body. A checkbox that flips before the store confirms it
 * is a mocked success, which is exactly what the house rules ban.
 *
 * Requirements (mini-PRD):
 *   ✔︎ 🎯 hydrateFeedback: one probe; 404 -> missing, success -> ok + all
 *     three surfaces loaded, other failures -> unknown and retriable.
 *     [if] a booting daemon permanently disables the chevron [then ⛔️] broken
 *   ✔︎ 🎯 setTodoDone/chooseTodoOption: PATCH immediately, state re-rendered
 *     from the response. [if] a failed PATCH leaves the box flipped [then ⛔️] broken
 *   ✔︎ 🎯 queueTodoFeedback/queueGeneralFeedback: debounced auto-save
 *     (AUTOSAVE_DEBOUNCE_MS trailing edge); flushFeedbackSaves() forces
 *     pending writes out on pagehide. [if] typed feedback still pending at
 *     pagehide is dropped [then ⛔️] broken
 *   ✔︎ 🎯 addPin: POST the pin, then re-render pins from the response.
 *     [if] a pin renders that the daemon never stored [then ⛔️] broken
 *   ✔︎ 🎯 archivePin: POST, and only then drop it from the canvas.
 *     [if] a pin leaves the canvas that the archive never received [then ⛔️] broken
 *   ✔︎ 🎯 submitFollowOn: POST the dedicated /follow-on endpoint so the
 *     parent reference is server-generated, never client-typed text.
 *     [if] a follow-on pin can lose its parent link [then ⛔️] broken
 *   ✔︎ 🎯 refreshPins: discards a snapshot that started before a local
 *     mutation landed, and retires the poll on a 404.
 *     [if] a poll in flight during an archive/add overwrites it with stale
 *     data, or a 404 polls forever [then ⛔️] broken
 */

import type { components } from "../api-types";
import { ApiError, api } from "../api/client";
import { makeDebounce, type Debounced } from "./feedback";

export type FeedbackTodo = components["schemas"]["TodoOut"];
export type FeedbackPin = components["schemas"]["CommentOut"];
export type FeedbackGeneralNote = components["schemas"]["GeneralNoteOut"];

export type FeedbackAvailability = "unknown" | "ok" | "missing";

export const AUTOSAVE_DEBOUNCE_MS = 600;
// Same-tab pin-status polling (issue #914 review, Wed 2 Sep 2026): hydrate
// runs once and never again, so an agent's PATCH while the reviewer keeps
// the app open never appeared until a full page reload. Matches
// usb-tracker.svelte.ts's POLL_MS for a live-but-not-noisy interval.
const PIN_POLL_MS = 5000;

interface FeedbackState {
  availability: FeedbackAvailability;
  todos: FeedbackTodo[];
  pins: FeedbackPin[];
  general: FeedbackGeneralNote | null;
  panelOpen: boolean;
  placementArmed: boolean;
  /** Last daemon refusal, verbatim; null while everything saves clean. */
  error: string | null;
}

export const feedbackState: FeedbackState = $state({
  availability: "unknown",
  todos: [],
  pins: [],
  general: null,
  panelOpen: false,
  placementArmed: false,
  error: null,
});

// ----- hydrate ------------------------------------------------------------
let hydrating: Promise<void> | null = null;

export async function hydrateFeedback(): Promise<void> {
  if (feedbackState.availability !== "unknown") return;
  hydrating ??= _hydrate().finally(() => {
    hydrating = null;
  });
  return hydrating;
}

async function _hydrate(): Promise<void> {
  try {
    const [todos, pins, general] = await Promise.all([
      api.GET("/api/v1/feedback/todos"),
      api.GET("/api/v1/feedback/comments"),
      api.GET("/api/v1/feedback/general"),
    ]);
    feedbackState.todos = todos.data?.todos ?? [];
    feedbackState.pins = pins.data?.comments ?? [];
    feedbackState.general = general.data ?? null;
    feedbackState.availability = "ok";
    feedbackState.error = null;
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) {
      // The daemon answered and does not serve feedback: settled fact.
      feedbackState.availability = "missing";
      return;
    }
    // Unreachable or 5xx: stay 'unknown' so a later toggle retries.
    feedbackState.error = err instanceof Error ? err.message : String(err);
  }
}

// ----- panel --------------------------------------------------------------
export function toggleFeedbackPanel(): void {
  if (feedbackState.availability !== "ok") {
    void hydrateFeedback();
    return;
  }
  feedbackState.panelOpen = !feedbackState.panelOpen;
}

export function armPinPlacement(): void {
  if (feedbackState.availability !== "ok") return;
  feedbackState.placementArmed = true;
}

export function disarmPinPlacement(): void {
  feedbackState.placementArmed = false;
}

// ----- todos --------------------------------------------------------------
function _upsertTodo(updated: FeedbackTodo): void {
  feedbackState.todos = feedbackState.todos.map((t) =>
    t.id === updated.id ? updated : t,
  );
}

async function _patchTodo(
  todoId: string,
  body: components["schemas"]["TodoPatchIn"],
): Promise<void> {
  try {
    const { data } = await api.PATCH("/api/v1/feedback/todos/{todo_id}", {
      params: { path: { todo_id: todoId } },
      body,
    });
    if (data) _upsertTodo(data);
    feedbackState.error = null;
  } catch (err) {
    feedbackState.error = err instanceof Error ? err.message : String(err);
  }
}

export function setTodoDone(todoId: string, done: boolean): Promise<void> {
  return _patchTodo(todoId, { done });
}

export function chooseTodoOption(
  todoId: string,
  option: string,
): Promise<void> {
  return _patchTodo(todoId, { chosen_option: option });
}

const todoFeedbackSavers = new Map<string, Debounced<string>>();

export function queueTodoFeedback(todoId: string, text: string): void {
  let saver = todoFeedbackSavers.get(todoId);
  if (saver === undefined) {
    saver = makeDebounce<string>(AUTOSAVE_DEBOUNCE_MS, (value) => {
      void _patchTodo(todoId, { feedback: value });
    });
    todoFeedbackSavers.set(todoId, saver);
  }
  saver.set(text);
}

// ----- general note -------------------------------------------------------
const generalSaver: Debounced<string> = makeDebounce<string>(
  AUTOSAVE_DEBOUNCE_MS,
  (text) => {
    void _putGeneral(text);
  },
);

async function _putGeneral(text: string): Promise<void> {
  try {
    const { data } = await api.PUT("/api/v1/feedback/general", {
      body: { text },
    });
    if (data) feedbackState.general = data;
    feedbackState.error = null;
  } catch (err) {
    feedbackState.error = err instanceof Error ? err.message : String(err);
  }
}

export function queueGeneralFeedback(text: string): void {
  generalSaver.set(text);
}

/** Force every pending debounced save out NOW (pagehide, panel close). */
export function flushFeedbackSaves(): void {
  for (const saver of todoFeedbackSavers.values()) saver.flush();
  generalSaver.flush();
}

// ----- pins ---------------------------------------------------------------
// Bumped on every local pin mutation (add/archive/follow-on). refreshPins()
// captures this before its GET and discards the response if it moved, so a
// poll that started before a mutation can never overwrite it with a stale
// snapshot once the mutation's own response has already landed.
let _pinGeneration = 0;

/** Insert `pin`, replacing any existing entry with the same id instead of
 * appending a second copy. A follow-on's own POST response can lose the race
 * with a `refreshPins()` GET that already picked up the same server-created
 * child (issue #914 review, Wed 3 Sep 2026): without this, the keyed pin loop
 * would render the same id twice until the next poll happened to repair it. */
function _upsertPin(pin: FeedbackPin): void {
  const idx = feedbackState.pins.findIndex((p) => p.id === pin.id);
  feedbackState.pins =
    idx === -1
      ? [...feedbackState.pins, pin]
      : feedbackState.pins.map((p, i) => (i === idx ? pin : p));
}

export async function addPin(
  pin: components["schemas"]["CommentCreateIn"],
): Promise<boolean> {
  try {
    const { data } = await api.POST("/api/v1/feedback/comments", { body: pin });
    if (data) {
      _pinGeneration++;
      _upsertPin(data);
    }
    feedbackState.error = null;
    return true;
  } catch (err) {
    feedbackState.error = err instanceof Error ? err.message : String(err);
    return false;
  }
}

/**
 * Archive ONE pin (issue #858). The daemon moves it into the archive file
 * with its full history; only once that returns does it leave the canvas -
 * dropping it locally first would hide a pin the archive never received.
 */
export async function archivePin(pinId: string): Promise<boolean> {
  try {
    await api.POST("/api/v1/feedback/comments/{comment_id}/archive", {
      params: { path: { comment_id: pinId } },
    });
    _pinGeneration++;
    feedbackState.pins = feedbackState.pins.filter((p) => p.id !== pinId);
    feedbackState.error = null;
    return true;
  } catch (err) {
    feedbackState.error = err instanceof Error ? err.message : String(err);
    return false;
  }
}

/**
 * Open a follow-on pin through the dedicated daemon endpoint (issue #914
 * review): the parent reference is generated server-side from the parent's
 * own issue_url/id, so it is never lost to whatever the reviewer typed or
 * deleted in the draft textarea. `text` is the reviewer's own addition,
 * appended after that generated reference; it may be empty.
 */
export async function submitFollowOn(
  parentId: string,
  text: string,
): Promise<boolean> {
  try {
    const { data } = await api.POST(
      "/api/v1/feedback/comments/{comment_id}/follow-on",
      {
        params: { path: { comment_id: parentId } },
        body: { text: text === "" ? null : text },
      },
    );
    if (data) {
      _pinGeneration++;
      _upsertPin(data);
    }
    feedbackState.error = null;
    return true;
  } catch (err) {
    feedbackState.error = err instanceof Error ? err.message : String(err);
    return false;
  }
}

// ----- pin polling (same-tab live refresh, issue #914 review) -------------
let _pinPollTimer: ReturnType<typeof setInterval> | null = null;

/** Re-GET pins and re-render. A transient miss leaves the board as it was;
 * the next tick retries, so one bad poll is silent rather than an error. A
 * 404 means the daemon no longer serves feedback at all (an older backend
 * attached mid-session) and retires the watch the same way hydrateFeedback
 * does, rather than polling a missing route forever. */
export async function refreshPins(): Promise<void> {
  if (feedbackState.availability !== "ok") return;
  const generation = _pinGeneration;
  try {
    const { data } = await api.GET("/api/v1/feedback/comments");
    // A mutation landed while this GET was in flight: its response is newer
    // than what this GET started from, so applying this snapshot now would
    // roll the board back.
    if (data && generation === _pinGeneration) feedbackState.pins = data.comments;
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) {
      feedbackState.availability = "missing";
      return;
    }
    // stays on the last-known board; polling itself never surfaces an error
  }
}

export function startPinWatch(): void {
  if (_pinPollTimer !== null) return;
  _pinPollTimer = setInterval(() => {
    if (feedbackState.availability === "missing") {
      stopPinWatch();
      return;
    }
    void refreshPins();
  }, PIN_POLL_MS);
}

export function stopPinWatch(): void {
  if (_pinPollTimer !== null) {
    clearInterval(_pinPollTimer);
    _pinPollTimer = null;
  }
}
