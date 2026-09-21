#!/usr/bin/env python3
"""Один синхронный сбор; не устанавливает расписание и не запускает модель."""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bamboo.analytics import pull, report
from bamboo.core import BambooError, lock


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--workspace", type=Path, required=True)
    p.add_argument("--provider", choices=["google", "yandex"], required=True)
    p.add_argument("--end")
    a = p.parse_args()
    try:
        end = date.fromisoformat(a.end) if a.end else datetime.now(timezone.utc).date() - timedelta(days=4 if a.provider == "google" else 1)
        root = a.workspace.resolve()
        with lock(root):
            collected = pull(root, a.provider, (end-timedelta(days=13)).isoformat(), end.isoformat())
            result = report(root, end.isoformat(), 7)
        print(json.dumps({"collection": collected, "report": result}, ensure_ascii=False, indent=2))
        return 0
    except (BambooError, OSError, ValueError, KeyError, TypeError) as e:
        print(str(e) if isinstance(e, BambooError) else type(e).__name__, file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
