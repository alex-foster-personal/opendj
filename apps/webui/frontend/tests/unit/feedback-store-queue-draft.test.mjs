/**
 * queuePinDraft / takePendingPinDraft (FB-12 / issue #698).
 *
 * One-shot handoff from QuickDraw into FeedbackWidget. Does not instantiate
 * Svelte; reuses the fetch-mock harness convention of feedback-store-poll.
 *
 * Regression lines:
 * - if availability !== 'ok' still stores a pending draft then a daemon
 *   without /api/v1/feedback would open a bubble that cannot save
 * - if queuePinDraft leaves placementArmed true then the overlay sits on
 *   top of the draft the menu just opened
 * - if takePendingPinDraft returns the same draft twice then a remount
 *   reopens a draft the widget already consumed
 * - if a second queuePinDraft keeps the first then a later right-click
 *   cannot replace a stale hit
 */

import assert from "node:assert/strict";
import { afterEach, before, test } from "node:test";

import { loadTypeScriptModule } from "./load-typescript.mjs";

const API_BASE = "https://feedback-queue-draft-api.example.test";

let store;

const draftA = {
  point: { x_pct: 10, y_pct: 20 },
  anchor: "vocal-area:abc@1.00:not_analyzed",
  text: "first",
  page: "/performance",
};

const draftB = {
  point: { x_pct: 30, y_pct: 40 },
  anchor: "vocal-area:abc@2.00:not_analyzed",
  text: "second",
  page: "/performance",
};

before(async () => {
  store = await loadTypeScriptModule("src/lib/rb/feedback-store.svelte.ts", {
    viteApiBase: API_BASE,
  });
});

afterEach(() => {
  store.feedbackState.availability = "unknown";
  store.feedbackState.pendingDraft = null;
  store.feedbackState.placementArmed = false;
});

test("queuePinDraft is a no-op when availability is not ok", () => {
  store.feedbackState.availability = "missing";
  store.feedbackState.pendingDraft = null;
  assert.equal(store.queuePinDraft(draftA), false);
  assert.equal(store.feedbackState.pendingDraft, null);

  store.feedbackState.availability = "unknown";
  assert.equal(store.queuePinDraft(draftA), false);
  assert.equal(store.feedbackState.pendingDraft, null);
});

test("queuePinDraft stores the draft and clears placementArmed when ok", () => {
  store.feedbackState.availability = "ok";
  store.feedbackState.placementArmed = true;
  assert.equal(store.queuePinDraft(draftA), true);
  assert.deepEqual(store.feedbackState.pendingDraft, draftA);
  assert.equal(store.feedbackState.placementArmed, false);
});

test("takePendingPinDraft returns the draft once and then null", () => {
  store.feedbackState.availability = "ok";
  store.queuePinDraft(draftA);
  assert.deepEqual(store.takePendingPinDraft(), draftA);
  assert.equal(store.feedbackState.pendingDraft, null);
  assert.equal(store.takePendingPinDraft(), null);
});

test("a second queuePinDraft replaces the first", () => {
  store.feedbackState.availability = "ok";
  store.queuePinDraft(draftA);
  store.queuePinDraft(draftB);
  assert.deepEqual(store.takePendingPinDraft(), draftB);
  assert.equal(store.takePendingPinDraft(), null);
});
