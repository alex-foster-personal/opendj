"""A stand-in for ffmpeg in the stream-encoder tests: copies stdin to stdout.

Writes a marker line on stderr at start. Env knobs:
FAKE_ENCODER_EXIT_AFTER_BYTES=<n> exits with FAKE_ENCODER_EXIT_CODE (default 3)
once n bytes are copied; FAKE_ENCODER_STALL=1 never reads stdin;
FAKE_ENCODER_IGNORE_EOF=1 keeps running after stdin closes.
"""

import os
import sys
import time

sys.stderr.write(f"fake-encoder: started {' '.join(sys.argv[1:])}\n")
sys.stderr.flush()
if os.environ.get("FAKE_ENCODER_STALL") == "1":
    while True:
        time.sleep(1)
exit_after = int(os.environ.get("FAKE_ENCODER_EXIT_AFTER_BYTES") or 0)
copied = 0
while data := os.read(0, 65536):
    os.write(1, data)
    copied += len(data)
    if exit_after and copied >= exit_after:
        sys.stderr.write("fake-encoder: exiting early\n")
        sys.stderr.flush()
        sys.exit(int(os.environ.get("FAKE_ENCODER_EXIT_CODE") or 3))
if os.environ.get("FAKE_ENCODER_IGNORE_EOF") == "1":
    while True:
        time.sleep(1)
