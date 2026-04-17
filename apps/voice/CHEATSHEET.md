# apps/voice -- printable cheat sheet

Tape this to the mixer. All phrases start with your wake word
(default `"hey booth"`).

## Read-only

| Say                       | Does                                    |
|---------------------------|-----------------------------------------|
| `find <artist or track>`  | Universal library search; event bus     |
| `what's the BPM`          | Speaks current deck's BPM               |
| `tempo`                   | Same as BPM                             |
| `what's the key`          | Speaks Camelot (or musical) key         |
| `next track`              | Advances the play queue                 |
| `mute voice`              | Mutes for 30 min                        |
| `unmute voice`            | Resumes                                 |

## Destructive (needs `--enable-destructive`)

| Say                                             | Does                                |
|-------------------------------------------------|-------------------------------------|
| `save that last transition as cue points`       | Writes cue points on both tracks    |
| `rate this N stars`  (1 <= N <= 5)              | Rates the current deck track        |

After a destructive command the daemon asks: "did you say X? yes or no."
Reply `yes` / `yeah` / `yep` / `confirm` -> action. Anything else
(including silence -- 3 s timeout) -> cancel.

## Failure modes

| Heard                          | Meaning                                   |
|--------------------------------|-------------------------------------------|
| "didn't get that"              | Grammar miss. Transcript logged.          |
| "no deck playing"              | Deck-state event not yet on the bus.      |
| "no recent transition to save" | Phase 12 hasn't reported a transition in the last 10 min. |
| "destructive mode off"         | You did not pass `--enable-destructive`.  |

## Operator commands

```bash
python -m apps.voice run --enable-destructive    # live mode
python -m apps.voice probe --text "find techno"  # no mic, just grammar
python -m apps.voice bench --iterations 50       # measure dispatch p50/p95
python -m apps.voice list-devices                # diagnose mic selection
```
