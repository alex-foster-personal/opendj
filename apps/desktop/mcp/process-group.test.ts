// Run: pnpm --dir apps/desktop exec tsx --test mcp/process-group.test.ts
// The post-exit group sweep: parsing is checked on captured `ps` text, ownership on
// real process groups whose leader has really exited. No test doubles.
import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { runningAsPosixRoot } from "./platform-identity";
import { groupHolder, groupMembers, ownsGroup, parsePs, resolvePs, sweepGroup, type Member } from "./process-group";

const POSIX = process.platform !== "win32";

test("ps lines parse into pid, group, state and start time; an unknown line is refused", () => {
  const members = parsePs(
    "    1     0 SLl  Thu Oct 22 13:36:47 2026\n" +
      " 4242  4240 Z+   Thu Oct 22 13:40:01 2026\n" +
      " 4243  4240 T    Thu Oct 22 13:40:02 2026\n\n",
  );
  assert.deepEqual(members, [
    { pid: 1, pgid: 0, started: "Thu Oct 22 13:36:47 2026", zombie: false, stopped: false },
    { pid: 4242, pgid: 4240, started: "Thu Oct 22 13:40:01 2026", zombie: true, stopped: false },
    { pid: 4243, pgid: 4240, started: "Thu Oct 22 13:40:02 2026", zombie: false, stopped: true },
  ]);
  assert.throws(() => parsePs("PID PGID STAT STARTED\n"), /does not know/);
});

test("a group is ours only while a remembered member is in it with the same start time", () => {
  const ours = { pid: 10, pgid: 7, started: "Thu Oct 22 13:00:00 2026", zombie: false, stopped: false };
  assert.equal(ownsGroup(7, [ours], [ours]), true);
  // The same pid started later is a reused pid, not the remembered process.
  assert.equal(ownsGroup(7, [ours], [{ ...ours, started: "Thu Oct 22 13:05:00 2026" }]), false);
  // Remembered, but no longer in that group.
  assert.equal(ownsGroup(7, [ours], [{ ...ours, pgid: 8 }]), false);
  assert.equal(ownsGroup(7, [], [ours]), false);
  assert.equal(ownsGroup(7, [ours], [{ ...ours, zombie: true }]), false);
});

/** A real group: a detached leader with one child, the leader then exited. */
async function groupWithExitedLeader(): Promise<{ pgid: number; remembered: ReturnType<typeof groupMembers> }> {
  const leader = spawn(
    process.execPath,
    [
      "-e",
      "const c = require('node:child_process').spawn(process.execPath, ['-e', 'setTimeout(() => {}, 120000)'], { stdio: 'ignore' });" +
        "c.unref(); process.stdout.write(String(c.pid) + '\\n'); process.stdin.once('data', () => process.exit(0));",
    ],
    { detached: true, stdio: ["pipe", "pipe", "ignore"] },
  );
  const childPid = await new Promise<number>((resolve) =>
    leader.stdout!.once("data", (d: Buffer) => resolve(Number(String(d).trim()))),
  );
  const pgid = leader.pid!;
  const remembered = groupMembers(pgid); // while the leader is alive
  assert.ok(remembered.some((m) => m.pid === childPid), "the child is listed in the leader's group");
  const exited = new Promise((r) => leader.once("exit", r));
  leader.stdin!.write("go\n");
  await exited;
  return { pgid, remembered };
}

const alive = (pid: number): boolean => {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
};

test("after the leader exits, a group a remembered member still holds is swept", { skip: !POSIX && "POSIX process groups" }, async () => {
  const { pgid, remembered } = await groupWithExitedLeader();
  const left = groupMembers(pgid);
  assert.ok(left.length > 0, "the child outlived its leader");
  try {
    assert.deepEqual(await sweepGroup(pgid, remembered), { swept: left.length });
    // swept means verified empty, not merely signalled.
    assert.deepEqual(groupMembers(pgid), []);
  } finally {
    for (const m of left) if (alive(m.pid)) process.kill(m.pid, "SIGKILL");
  }
});

test("after the leader exits, a group nothing remembered proves is left alone (Codex on #6830)", { skip: !POSIX && "POSIX process groups" }, async () => {
  // What a reused group id looks like from here: live members, none of them remembered.
  const { pgid } = await groupWithExitedLeader();
  const left = groupMembers(pgid);
  try {
    const sweep = await sweepGroup(pgid, []);
    assert.deepEqual(sweep, { leftAlone: left.map((m) => m.pid) });
    await new Promise((r) => setTimeout(r, 200));
    for (const m of left) assert.ok(alive(m.pid), `if ${m.pid} was killed then broken`);
  } finally {
    for (const m of left) if (alive(m.pid)) process.kill(m.pid, "SIGKILL");
  }
});

test("a holder that is gone once stopped proves nothing: the group is left alone and nothing stays stopped (Sol on #6830)", { skip: !POSIX && "POSIX process groups" }, async () => {
  // The race the stop closes: the remembered holder exits between the ownership check
  // and the kill. Here it really is killed at that moment, from inside the re-listing.
  const { pgid, remembered } = await groupWithExitedLeader();
  const left = groupMembers(pgid);
  const holder = groupHolder(pgid, remembered, left);
  assert.ok(holder, "the child holds the group before the sweep");
  let listings = 0;
  const relist = (id: number): Member[] => {
    listings += 1;
    if (listings === 2) {
      process.kill(holder.pid, "SIGKILL");
      while (groupMembers(id).some((m) => m.pid === holder.pid)) {
        /* wait for the kernel to finish it */
      }
    }
    return groupMembers(id);
  };
  try {
    const sweep = await sweepGroup(pgid, remembered, relist);
    assert.equal(listings, 2, "the holder was re-checked after it was stopped");
    assert.ok("leftAlone" in sweep, `left alone, not ${JSON.stringify(sweep)}`);
  } finally {
    for (const m of left) if (alive(m.pid)) process.kill(m.pid, "SIGKILL");
  }
});

test("a holder whose stop never lands is not trusted to pin the group (SIGSTOP is asynchronous)", { skip: !POSIX && "POSIX process groups" }, async () => {
  const { pgid, remembered } = await groupWithExitedLeader();
  const left = groupMembers(pgid);
  assert.ok(groupHolder(pgid, remembered, left), "the child holds the group before the sweep");
  let listings = 0;
  // Every listing after the stop reports the holder still running, as a stop in flight would.
  const running = (id: number): Member[] => {
    listings += 1;
    return groupMembers(id).map((m) => ({ ...m, stopped: false }));
  };
  try {
    const started = Date.now();
    const sweep = await sweepGroup(pgid, remembered, running);
    assert.ok("leftAlone" in sweep, `left alone, not ${JSON.stringify(sweep)}`);
    assert.ok(listings > 2, "control: the sweep kept waiting for the stop to land");
    assert.ok(Date.now() - started >= 900, "and waited out the bound before giving up");
    await new Promise((r) => setTimeout(r, 100));
    for (const m of left) assert.ok(alive(m.pid), `if ${m.pid} was killed then broken`);
    const state = spawnSync(resolvePs(), ["-o", "stat=", "-p", String(left[0].pid)], { encoding: "utf8" }).stdout;
    assert.doesNotMatch(state.trim(), /^T/, "and the holder was resumed");
  } finally {
    for (const m of left) if (alive(m.pid)) process.kill(m.pid, "SIGKILL");
  }
});

test("a relist that throws after the stop resumes the holder before the error surfaces (Codex on #6830)", { skip: !POSIX && "POSIX process groups" }, async () => {
  const { pgid, remembered } = await groupWithExitedLeader();
  const left = groupMembers(pgid);
  const holder = groupHolder(pgid, remembered, left);
  assert.ok(holder, "the child holds the group before the sweep");
  const state = (pid: number): string =>
    spawnSync(resolvePs(), ["-o", "stat=", "-p", String(pid)], { encoding: "utf8" }).stdout.trim();
  let listings = 0;
  let stoppedAtThrow = "";
  const relist = (id: number): Member[] => {
    listings += 1;
    if (listings === 2) {
      stoppedAtThrow = state(holder.pid);
      throw new Error("ps failed");
    }
    return groupMembers(id);
  };
  try {
    await assert.rejects(sweepGroup(pgid, remembered, relist), /ps failed/);
    assert.match(stoppedAtThrow, /^T/, "control: the holder really was stopped when the relist threw");
    assert.doesNotMatch(state(holder.pid), /^T/, "the holder runs again");
  } finally {
    for (const m of left) if (alive(m.pid)) process.kill(m.pid, "SIGKILL");
  }
});

test("a refused resume is surfaced, alongside the error that led to it, never swallowed (Sol on #6830)", { skip: !POSIX && "POSIX process groups" }, async () => {
  const { pgid, remembered } = await groupWithExitedLeader();
  const left = groupMembers(pgid);
  const holder = groupHolder(pgid, remembered, left);
  assert.ok(holder, "the child holds the group before the sweep");
  const realKill = process.kill.bind(process);
  let refusedResumes = 0;
  process.kill = ((pid: number, signal?: string | number) => {
    if (signal === "SIGCONT") {
      refusedResumes += 1;
      throw Object.assign(new Error("kill EPERM"), { code: "EPERM" });
    }
    return realKill(pid, signal);
  }) as typeof process.kill;
  try {
    const throwing = (): Member[] => {
      throw new Error("ps failed");
    };
    const twice = (() => {
      let n = 0;
      return (id: number): Member[] => (++n === 2 ? throwing() : groupMembers(id));
    })();
    await assert.rejects(sweepGroup(pgid, remembered, twice), (error: unknown) => {
      assert.ok(error instanceof AggregateError, `an AggregateError, not ${String(error)}`);
      assert.deepEqual(
        error.errors.map((e: Error) => e.message),
        ["ps failed", "kill EPERM"],
      );
      return true;
    });
    assert.equal(refusedResumes, 1, "control: the resume really was attempted and refused");
  } finally {
    process.kill = realKill;
    for (const m of left) if (alive(m.pid)) process.kill(m.pid, "SIGKILL");
  }
});

test("ps is resolved by path, and a missing one is refused rather than looked up on PATH", { skip: !POSIX && "POSIX ps" }, () => {
  assert.match(resolvePs(), /^\/(usr\/)?bin\/ps$/);
  assert.throws(() => resolvePs(["/nonexistent/ps"]), /no ps at \/nonexistent\/ps/);
});

test("a group kill refused for a reason other than the group being gone throws (Codex on #6830)", { skip: (!POSIX && "POSIX process groups") || (!runningAsPosixRoot() && "needs root, to probe a root-owned group as an unprivileged user") }, async () => {
  // An unprivileged process sweeping a root-owned group it was told is its own: the
  // kernel refuses the SIGKILL with EPERM, which must surface, never read as swept.
  // The target is this test runner's own group, so nothing outside it is at stake.
  const pgid = Number(
    spawnSync(resolvePs(), ["-o", "pgid=", "-p", String(process.pid)], { encoding: "utf8" }).stdout.trim(),
  );
  assert.ok(pgid > 1, `a real group id, never 0 or 1 (read ${pgid})`);
  const script = `
    import { groupMembers, sweepGroup } from ${JSON.stringify(new URL("./process-group.ts", import.meta.url).href)};
    sweepGroup(${pgid}, groupMembers(${pgid})).then(
      (r) => { console.log("RESOLVED " + JSON.stringify(r)); process.exit(0); },
      (e) => { console.log("REJECTED " + e.code); process.exit(0); },
    );`;
  // fileURLToPath, not .pathname: a checkout path with a space or # stays percent-encoded there.
  const tsx = fileURLToPath(new URL("../node_modules/.bin/tsx", import.meta.url));
  // Its own group (detached), so the probe is not itself a member of the group it signals.
  const probe = spawn(tsx, ["--eval", script], {
    detached: true,
    uid: 65534,
    gid: 65534,
    env: { ...process.env, HOME: tmpdir() },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let out = "";
  let err = "";
  probe.stdout!.on("data", (d) => (out += d));
  probe.stderr!.on("data", (d) => (err += d));
  const code = await new Promise<number | null>((r) => probe.once("exit", r));
  assert.equal(code, 0, `the probe ran (stderr: ${err})`);
  assert.match(out, /^REJECTED EPERM$/m);
});
