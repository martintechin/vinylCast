"""R4 auto-cast evaluation.

    python scripts/r4_summary.py FILE.jsonl [--idle-from HH:MM --idle-to HH:MM]

Lists each needle-drop cycle (music -> speaker playing, how it ended, silence -> stop, side
flips) and checks PLAN-rev1's R4 criteria. Exit 0 = all checks pass. Logic in
vinylcast/autocast_report.py.
"""

import sys

from vinylcast.autocast_report import main

sys.exit(main())
