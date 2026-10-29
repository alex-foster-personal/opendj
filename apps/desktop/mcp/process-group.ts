// POSIX process-group ownership for the MCP smoke and the real-CDP tests.
//
// A group id is the leader's pid. Once the leader has exited, that number stays
// reserved only while some process is still in the group; when the group empties,
// the kernel may hand the number to an unrelated process that makes itself a group
// leader. So after the leader exits, `kill(-pgid)` is safe only while a process
// remembered from this run is provably still in the group (Codex on #6830). This
// mirrors `group_held_by` in apps/shared/process_groups.py.
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";

/** Where POSIX systems keep `ps`; resolved by path, never looked up on PATH (Codex on #6830). */
const PS_PATHS = ["/bin/ps", "/usr/bin/ps"];

/** The `ps` to run, or why there is none. */
export function resolvePs(candidates: readonly string[] = PS_PATHS): string {
  const found = candidates.find((path) => existsSync(path));
  if (!found) throw new Error(`no ps at ${candidates.join(" or ")}, so process groups cannot be measured`);
  return found;
}

/** One process, as `ps` lists it. `started` is its start time, which tells a reused pid apart. */
export interface Member {
  pid: number;
  pgid: number;
  started: string;
  zombie: boolean;
  /** Job-control stopped (state T): a SIGSTOP has landed, not merely been sent. */
  stopped: boolean;
}

/** Parses `ps -A -o pid=,pgid=,stat=,lstart=`; a line that does not fit is refused, never skipped. */
export function parsePs(text: string): Member[] {
  const members: Member[] = [];
  for (const line of text.split("\n")) {
    if (!line.trim()) continue;
    const match = /^\s*(\d+)\s+(\d+)\s+(\S+)\s+(.+?)\s*$/.exec(line);
    if (!match) throw new Error(`ps printed a line this parser does not know: ${JSON.stringify(line)}`);
    members.push({
      pid: Number(match[1]),
      pgid: Number(match[2]),
      started: match[4],
      zombie: match[3].startsWith("Z"),
      stopped: match[3].startsWith("T"),
    });
  }
  return members;
}

/** Every live (non-zombie) process now in group `pgid`. Throws when `ps` cannot measure. */
export function groupMembers(pgid: number): Member[] {
  const ps = spawnSync(resolvePs(), ["-A", "-o", "pid=,pgid=,stat=,lstart="], { encoding: "utf8" });
  if (ps.error || ps.status !== 0)
    throw new Error(`ps could not list processes (${ps.error?.message ?? `exit ${ps.status}`}: ${ps.stderr})`);
  const all = parsePs(ps.stdout);
  if (all.length === 0) throw new Error("ps listed no processes at all, so it measured nothing");
  return all.filter((m) => m.pgid === pgid && !m.zombie);
}

/**
 * The remembered process that proves group `pgid` is the one `remembered` was taken
 * from: still in it, with the same pid and start time. While it is, the group never
 * emptied, so its id was never free to be reused. Undefined when none does.
 */
export function groupHolder(pgid: number, remembered: readonly Member[], now: readonly Member[]): Member | undefined {
  return now.find(
    (m) => m.pgid === pgid && !m.zombie && remembered.some((r) => r.pid === m.pid && r.started === m.started),
  );
}

/** Whether a remembered process still holds group `pgid` (see `groupHolder`). */
export function ownsGroup(pgid: number, remembered: readonly Member[], now: readonly Member[]): boolean {
  return groupHolder(pgid, remembered, now) !== undefined;
}

/** How long a swept group gets to empty after SIGKILL before its survivors are reported. */
const SWEEP_WAIT_MS = 5_000;
/** How long the sweep waits for a SIGSTOP it sent to land before it trusts the holder. */
export const PIN_STOP_WAIT_MS = 1_000;

const sameProcess = (a: Member, b: Member): boolean => a.pid === b.pid && a.started === b.started;

/**
 * Re-lists group `pgid` until `holder` shows stopped, it has left the group, or
 * `PIN_STOP_WAIT_MS` passes. SIGSTOP is delivered asynchronously, so until `ps` reports
 * the holder stopped it may still run, leave the group and free the id.
 */
async function relistOnceStopped(
  pgid: number,
  holder: Member,
  relist: (pgid: number) => Member[],
): Promise<Member[]> {
  const deadline = Date.now() + PIN_STOP_WAIT_MS;
  for (;;) {
    const now = relist(pgid);
    const seen = now.find((m) => sameProcess(m, holder));
    if (!seen || seen.stopped || Date.now() > deadline) return now;
    await new Promise((r) => setTimeout(r, 5));
  }
}

/**
 * After the leader of group `pgid` has exited, SIGKILL what is left of the group, but
 * only when `remembered` (taken while the leader was alive) proves the group is still
 * ours, and only while a stopped remembered process keeps the id reserved. Returns what
 * was done: swept (and the group is verified empty), left alone (unproven), or survived
 * (still there after the kill). A signal refused for any reason but its target already
 * being gone throws (Codex on #6830).
 */
export async function sweepGroup(
  pgid: number,
  remembered: readonly Member[],
  relist: (pgid: number) => Member[] = groupMembers,
): Promise<{ swept: number } | { leftAlone: number[] } | { survived: number[] }> {
  // kill(-1) signals every process the caller may signal, and -0 its own group.
  if (!Number.isInteger(pgid) || pgid <= 1) throw new Error(`refusing to sweep process group ${pgid}`);
  const now = relist(pgid);
  if (now.length === 0) return { swept: 0 };
  const holder = groupHolder(pgid, remembered, now);
  if (!holder) return { leftAlone: now.map((m) => m.pid) };
  // A check and a later kill(-pgid) are not atomic: the holder could exit in between,
  // empty the group and free its id (Sol on #6830). So the holder is stopped first. A
  // stopped process cannot exit on its own, so once it is confirmed still to be the
  // remembered process in this group, the group id stays reserved through the kill.
  if (!signalled(holder.pid, "SIGSTOP")) return { leftAlone: now.map((m) => m.pid) };
  // Until the group kill has been sent, every way out, a throw included, resumes the
  // holder: a process left stopped keeps its ports and profile files (Codex on #6830).
  let pinned: Member[] = [];
  let early: { leftAlone: number[] } | { swept: number } | undefined;
  let failure: unknown;
  let failed = false;
  try {
    pinned = await relistOnceStopped(pgid, holder, relist);
    if (!groupHolder(pgid, [holder], pinned)?.stopped) {
      // The pid stopped is no longer the holder (it exited, or the number was reused), or
      // the stop never landed: let whatever has it run on, and touch nothing in a group
      // not proven ours.
      early = { leftAlone: pinned.map((m) => m.pid) };
    } else if (!signalled(-pgid, "SIGKILL")) {
      // The holder is in the group, so the kill reaches it too, stopped or not.
      early = { swept: 0 };
    }
  } catch (error) {
    failed = true;
    failure = error;
  }
  if (failed || early) {
    // A resume refused for any reason but the holder being gone leaves it frozen,
    // so it is never swallowed, and never hides the error that got here (Sol on #6830).
    try {
      signalled(holder.pid, "SIGCONT");
    } catch (resumeError) {
      throw failed
        ? new AggregateError([failure, resumeError], `could not resume stopped process ${holder.pid}`)
        : resumeError;
    }
    if (failed) throw failure;
    return early!;
  }
  const deadline = Date.now() + SWEEP_WAIT_MS;
  for (;;) {
    const left = groupMembers(pgid);
    if (left.length === 0) return { swept: pinned.length };
    if (Date.now() > deadline) return { survived: left.map((m) => m.pid) };
    await new Promise((r) => setTimeout(r, 50));
  }
}

/** Sends `signal`; false when the target is already gone. Any other refusal throws (Codex on #6830). */
function signalled(target: number, signal: NodeJS.Signals): boolean {
  try {
    process.kill(target, signal);
    return true;
  } catch (error) {
    // Only "no such process" means the target exited before the signal.
    if ((error as NodeJS.ErrnoException).code === "ESRCH") return false;
    throw error;
  }
}
