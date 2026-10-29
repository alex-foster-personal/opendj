---
term: Mutation test
slug: mutation-test
aliases: [mutation testing, mutant, fault injection, mutate the guard]
group: Evaluation and method
level: 2
one_liner: Break the thing on purpose and check the test goes red; a check you have never seen fail has not been shown to work.
why_it_matters_here: Every quality measure we score transitions with must drop when we inject a known defect, or it is not measuring that defect.
related: [noise-band-and-bootstrap, external-validation, veto-vs-weighted-score, beat-grid]
sources:
  - DeMillo, Lipton and Sayward, "Hints on Test Data Selection", IEEE Computer 11(4), 1978
  - Jia and Harman, "An Analysis and Survey of the Development of Mutation Testing", IEEE Transactions on Software Engineering 37(5), 2011
  - Wikipedia, "Mutation testing" (secondary pointer)
status: draft
---

## In plain words

A test that passes proves little until you have watched it fail. Mutation testing makes the
failure happen on purpose: change the code or the input in one small, known way (a "mutant"),
run the check, and expect it to go red. If it stays green, the mutant "survived", and that is a
finding: the check carries an assumption nothing verifies. The same idea applies to a quality
measure. Take a good transition and inject a defect a DJ would hear (shift deck B by 30 ms,
bring it in one beat late, clash two vocals, add a click at a loop seam), then score it again.
The score must drop by more than its noise band, or the measure is blind to that defect. Mutate
the opposite way too: a fix that overshoots (punishing every vocal overlap, say) also needs a
control that shows the original good behavior still scores well.

## Picture

```text
  good transition             known defect injected
  +----------------+  mutate  +------------------------+
  |  M = 0.82      | -------> |  deck B shifted 30 ms  |
  +----------------+          +------------------------+
                                          |
                                          v
                              +------------------------+
                              |  score it again        |
                              +------------------------+
                                          |
                  +-----------------------+---------------------+
                  |                                             |
        M drops past the noise band               M barely moves
        (mutant caught)                           (mutant survives)
                  |                                             |
                  v                                             v
        the measure sees this defect           the measure is blind,
                                               fix the measure first
```

## In Open DJ

The project rule lives in `(private path)` under "Mutate the guard, in BOTH
directions". `scripts/sync_drift_mutation_control.py` is a worked example: it makes each check
in a linter return nothing before measuring, one at a time, and requires that check's own tests
to fail. For transitions, the macro quality measure research (Tue 27 Oct 2026) lists mutation
tests of the measure itself as an external validation that costs no human time: a beat offset, a
one-beat late entry, a vocal clash and a loop-seam click must each drop M by at least the
fixture's noise band before the measure is used to judge anything.
