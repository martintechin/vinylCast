"""Summarise a soak-test run: python scripts/soak_summary.py METRICS.jsonl [--run N]

Prints CPU per process, event counts, startup latency, Wi-Fi/throttling/peak data and the
kernel's USB-loss messages for the run window, with PASS/FAIL against PLAN.md's M5 criteria.
Exit code 0 = PASS, 1 = FAIL. The logic lives in vinylcast/soak.py.
"""

import sys

from vinylcast.soak import main

sys.exit(main())
