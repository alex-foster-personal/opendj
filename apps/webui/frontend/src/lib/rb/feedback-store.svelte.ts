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
 */

import type { components } from "../api-types";
import { ApiError, api } from "../api/client";
import { makeDebounce, type Debounced } from "./feedback";

export type FeedbackTodo = components["schemas"]["TodoOut"];
export type FeedbackPin = components["schemas"]["CommentOut"];
export type FeedbackGeneralNote = components["schemas"]["GeneralNoteOut"];

export type FeedbackAvailability = "unknown" | "ok" | "missing";

export const AUTOSAVE_DEBOUNCE_MS = 600;

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
export async function addPin(
  pin: components["schemas"]["CommentCreateIn"],
): Promise<boolean> {
  try {
    const { data } = await api.POST("/api/v1/feedback/comments", { body: pin });
    if (data) feedbackState.pins = [...feedbackState.pins, data];
    feedbackState.error = null;
    return true;
  } catch (err) {
    feedbackState.error = err instanceof Error ? err.message : String(err);
    return false;
  }
}
