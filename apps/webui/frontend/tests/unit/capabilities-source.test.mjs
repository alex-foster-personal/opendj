import assert from "node:assert/strict";
import { test } from "node:test";

import {
  assertCapabilityRefusalsAreModuleConstants,
  readCapabilitiesSource,
} from "./capabilities-source.mjs";

test("inlined capability refusal literals fail the module-constant guard", () => {
  const real = readCapabilitiesSource();
  const inlined = real.replace(
    /return capabilities\.flavor === 'legacy' \? JOBS_MISSING : UNIDENTIFIED;/,
    "return capabilities.flavor === 'legacy' ? 'jobs API not offered by this daemon (no /api/v1/jobs on a legacy boot)' : UNIDENTIFIED;",
  );
  assert.throws(
    () => assertCapabilityRefusalsAreModuleConstants(inlined),
    /string literal|module constant|inlined/i,
  );
});
