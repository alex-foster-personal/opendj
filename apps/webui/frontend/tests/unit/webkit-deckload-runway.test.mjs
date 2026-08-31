import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

// a3807925 - the webkit-deckload suite's playhead runway.
//
// THE BUG THIS WATCHES, because it cost two investigations and neither found
// it: the suite plays deck 1 continuously from the double-click load onward,
// and its fixture audio is 60s. Nothing returned the playhead, so cumulative
// playing time (~68s on a fast machine) walked the deck off the END of the
// track partway through the run. The app was correct throughout - auto-play
// looked for a successor, found none, and the deck stopped - but every later
// `_waitForAudible` then timed out against a deck that was loaded and simply
// out of audio, which reads as "the deck is broken".
//
// WHICH test wore that failure depended on nothing but machine speed. It was
// reported as the tempo test, reproduced on clean trunk as the loop test, and
// seen as play/pause on a slower run of the same worktree. A moving test name
// is the signature of this whole defect class, and it is invisible to every
// gate in the repo: the e2e suite passes on a fast machine, so nothing goes
// red until someone else's machine is slower.
//
// These are source-shape guards for exactly that reason. The suite cannot
// catch its own regression here - a rewind that is deleted simply makes the
// suite speed-dependent again, silently, until it fails somewhere else.
//
// Regression lines:
// - if the beforeEach stops seeking deck 1 back to the start then every test
//   again inherits how long the tests before it took, and the failure moves
//   between machines instead of pointing at a cause
// - if _sampleAdvanceMs times itself across Playwright round trips again then
//   a correctly playing deck reports inflated travel on a loaded machine
//   (3201ms measured for a nominal 1500ms window, against a 2250ms bound)
// - if the CUE test stops travelling a deliberate distance then its cue point
//   is once more whatever the previous tests happened to leave behind
// - if _waitForAudible stops naming the playhead it gave up on then a dry deck
//   and a dead deck are again indistinguishable without opening a trace
// - if the fixture audio is shortened then the runway argument these guards
//   protect stops holding, and the suite returns to running dry mid-run

const E2E = fileURLToPath(new URL("../e2e", import.meta.url));

/**
 * Read source with LF endings whatever the checkout used. These guards match
 * multi-line shapes with `\n`, and a Windows checkout carries CRLF - the guard
 * would then fail on line endings rather than on the contract it watches.
 */
function readE2eSource(relative) {
  return readFileSync(`${E2E}/${relative}`, "utf8").replaceAll("\r\n", "\n");
}

const SPEC_SRC = readE2eSource("webkit-deckload.spec.ts");
const FIXTURE_SRC = readE2eSource("support/deckload_fixture.py");

/**
 * The body of one top-level function in the spec.
 *
 * Sliced to the next brace in column 0 rather than brace-matched: every
 * function this guard reads is declared at top level, so that IS the close,
 * and a real parser here would be more machinery than the contract needs.
 */
function specFunctionBody(name) {
  const start = SPEC_SRC.indexOf(`async function ${name}(`);
  assert.notEqual(
    start,
    -1,
    `webkit-deckload.spec.ts no longer defines ${name}`,
  );
  const end = SPEC_SRC.indexOf("\n}", start);
  assert.notEqual(end, -1, `${name} has no top-level close brace`);
  return SPEC_SRC.slice(start, end);
}

test("every test starts deck 1 from a known playhead", () => {
  const beforeEach = SPEC_SRC.indexOf("test.beforeEach(");
  assert.notEqual(
    beforeEach,
    -1,
    "the suite has no beforeEach, so nothing returns the playhead between " +
      "tests and the run is speed-dependent again",
  );
  const body = SPEC_SRC.slice(
    beforeEach,
    SPEC_SRC.indexOf("\n\t});", beforeEach),
  );
  assert.match(
    body,
    /_seek\(page,\s*1,\s*0\)/,
    "the beforeEach no longer rewinds deck 1 to the start of the track",
  );
  // The load test is the one that puts a track on the deck, so the rewind has
  // to tolerate a deck that has none. Without this the suite fails on its very
  // first test instead of running.
  assert.match(
    body,
    /stable_id === null/,
    "the beforeEach no longer skips a deck with no track loaded",
  );
});

test("the transport sample measures its own window", () => {
  const body = specFunctionBody("_sampleAdvanceMs");
  assert.match(
    body,
    /_measureRate\(/,
    "_sampleAdvanceMs no longer routes through _measureRate, which is the " +
      "only helper here that times itself inside the page",
  );
  // The original defect precisely: two `_query` round trips straddling a
  // `waitForTimeout`, with the round-trip cost charged to the audio clock.
  assert.doesNotMatch(
    body,
    /waitForTimeout/,
    "_sampleAdvanceMs waits in node again, so Playwright round-trip latency " +
      "is once more counted as playhead travel",
  );
});

test("the CUE test travels a deliberate distance before stamping", () => {
  assert.match(
    SPEC_SRC,
    /const CUE_RUNWAY_MS = /,
    "CUE_RUNWAY_MS is gone, so the cue point is back to whatever distance the " +
      "previous tests happened to leave on the playhead",
  );
  assert.match(
    SPEC_SRC,
    /_playUntilPast\(page,\s*1,\s*CUE_RUNWAY_MS\)/,
    "the CUE test no longer plays to CUE_RUNWAY_MS before pausing",
  );
});

test("a wait for audible names the playhead it gave up on", () => {
  const body = specFunctionBody("_waitForAudible");
  assert.match(
    body,
    /catch/,
    "_waitForAudible raises a bare Playwright timeout again, which is what " +
      "made a dry deck read as a broken one",
  );
  for (const field of ["position", "duration_ms", "transport_pending"]) {
    assert.ok(
      body.includes(field),
      `_waitForAudible no longer reports ${field}, so a dry deck and a dead ` +
        "deck are indistinguishable from the failure message",
    );
  }
});

test("the fixture audio is long enough to give every test a runway", () => {
  const lengths = [...FIXTURE_SRC.matchAll(/seconds=(\d+(?:\.\d+)?)/g)].map(
    (m) => Number(m[1]),
  );
  assert.ok(
    lengths.length > 0,
    "deckload_fixture.py declares no track lengths",
  );
  // The longest single test in the suite plays for well under 30s now that
  // each one starts from zero. This floor is the margin that claim rests on:
  // shorten the fixture past it and the suite can run dry inside ONE test,
  // which no rewind can fix.
  for (const seconds of lengths) {
    assert.ok(
      seconds >= 30,
      `a fixture track is ${seconds}s, too short for one test's runway`,
    );
  }
});
