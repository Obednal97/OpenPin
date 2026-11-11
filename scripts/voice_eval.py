#!/usr/bin/env python3
"""Show how well voice matching would do on your own library, per threshold.

Uses cached voice embeddings only (never audio). Reads the app's data folder, or
the one given with --data-dir. Same instances and rules as the in-app calibration.
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", help="the Open Pin data folder (default: the app's own)")
    args = parser.parse_args(argv)
    if args.data_dir:
        os.environ["PLAUD_DATA_DIR"] = args.data_dir  # before app.utils.constants is imported
    from app.people import backlog, matching
    from app.people import store as people_store

    instances = backlog.instances()
    people = people_store.load()["people"]
    print(f"{len(instances)} named instances from {len({i[3] for i in instances})} people")
    print(f"{'threshold':>9} {'named':>6} {'right':>6} {'wrong':>6} {'wrong %':>8}")
    for row in matching.threshold_table(instances, people):
        wrong = row["named"] - row["right"]
        pct = f"{100 * wrong / row['named']:.1f}" if row["named"] else "-"
        print(f"{row['threshold']:>9.3f} {row['named']:>6} {row['right']:>6} {wrong:>6} {pct:>8}")
    result = matching.calibrate(instances, people)
    print(f"calibrate would pick {result['threshold']:.3f} (named {result['named']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
