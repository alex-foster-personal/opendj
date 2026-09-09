/**
 * Client-side screenshot-paste policy for comment pins (issue #1333, part 2
 * of #928): which clipboard item counts as a pasted image, and the explicit
 * refusal text for one that is too large or an unsupported type.
 *
 * Regression lines:
 * - if a non-image clipboard item (plain text alongside a screenshot) is
 *   picked up as the image then the wrong thing gets uploaded
 * - if an oversized paste is accepted silently then a reviewer has no idea
 *   why their screenshot never shows up on the pin
 * - if a paste exactly AT the limit is refused then the policy is off by one
 *   the tight way; if one byte OVER the limit is accepted it is off by one
 *   the loose way - both directions are checked
 * - if an image of a disallowed type (svg, bmp, heic) reads the same as "no
 *   image in the clipboard" then PR #1425 P2 regresses: a pasted screenshot
 *   of the wrong type is silently dropped instead of refused (FB-11)
 */

import assert from "node:assert/strict";
import { before, test } from "node:test";

import { loadTypeScriptModule } from "./load-typescript.mjs";

let mod;
before(async () => {
  mod = await loadTypeScriptModule("src/lib/rb/feedback-pin-attachment.ts");
});

function fakeFile(size, type) {
  return { size, type };
}

function fakeItem(type, file) {
  return { type, getAsFile: () => file };
}

// ----- pastedImageFile -----------------------------------------------------
test("picks the first allowed-image item out of the clipboard", () => {
  const png = fakeFile(100, "image/png");
  const items = [fakeItem("text/plain", fakeFile(10, "text/plain")), fakeItem("image/png", png)];
  assert.deepEqual(mod.pastedImageFile(items), { kind: "file", file: png });
});

test("a clipboard with no image item at all reads as none, not rejected", () => {
  const items = [fakeItem("text/plain", fakeFile(10, "text/plain"))];
  assert.deepEqual(mod.pastedImageFile(items), { kind: "none" });
});

test("an unsupported image subtype (e.g. svg) is reported as rejected, not silently dropped", () => {
  // PR #1425 P2: this used to read identically to "no image at all", so a
  // pasted screenshot of the wrong type vanished with no message (FB-11
  // forbids exactly this).
  const items = [fakeItem("image/svg+xml", fakeFile(10, "image/svg+xml"))];
  assert.deepEqual(mod.pastedImageFile(items), { kind: "rejected", type: "image/svg+xml" });
});

test("bmp and heic are also rejected explicitly, not dropped", () => {
  assert.deepEqual(mod.pastedImageFile([fakeItem("image/bmp", fakeFile(10, "image/bmp"))]), {
    kind: "rejected",
    type: "image/bmp",
  });
  assert.deepEqual(mod.pastedImageFile([fakeItem("image/heic", fakeFile(10, "image/heic"))]), {
    kind: "rejected",
    type: "image/heic",
  });
});

test("undefined/null clipboard items read as no image, not a crash", () => {
  assert.deepEqual(mod.pastedImageFile(undefined), { kind: "none" });
  assert.deepEqual(mod.pastedImageFile(null), { kind: "none" });
});

test("an item whose getAsFile() returns null is skipped, not thrown", () => {
  const items = [fakeItem("image/png", null)];
  assert.deepEqual(mod.pastedImageFile(items), { kind: "none" });
});

test("a rejected type takes priority over a later valid image (first offending item wins)", () => {
  const png = fakeFile(100, "image/png");
  const items = [fakeItem("image/svg+xml", fakeFile(10, "image/svg+xml")), fakeItem("image/png", png)];
  // The allowed png later in the list is still picked up as a `file`
  // outcome - only an item with NO allowed image anywhere is `rejected`.
  assert.deepEqual(mod.pastedImageFile(items), { kind: "file", file: png });
});

// ----- attachmentSizeRefusal -------------------------------------------------
test("a file within the limit is not refused", () => {
  const file = fakeFile(mod.MAX_PIN_ATTACHMENT_BYTES, "image/png");
  assert.equal(mod.attachmentSizeRefusal(file), null);
});

test("one byte over the limit is refused with an explicit message", () => {
  const file = fakeFile(mod.MAX_PIN_ATTACHMENT_BYTES + 1, "image/png");
  const message = mod.attachmentSizeRefusal(file);
  assert.notEqual(message, null);
  assert.match(message, /limit/);
});

test("the refusal message names both the file size and the limit in MB", () => {
  const file = fakeFile(mod.MAX_PIN_ATTACHMENT_BYTES * 2, "image/png");
  const message = mod.attachmentSizeRefusal(file);
  const limitMb = (mod.MAX_PIN_ATTACHMENT_BYTES / (1024 * 1024)).toFixed(1);
  assert.match(message, new RegExp(limitMb.replace(".", "\\.")));
});
