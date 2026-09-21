#!/usr/bin/env python3
"""Сгенерировать или проверить нативные точки входа из канонических файлов."""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from bamboo.install import SYSTEMS, adapters, replace_block
from bamboo.core import write_text


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    files, blocks = adapters(ROOT, SYSTEMS, ".")
    for rel, managed in blocks.items():
        p = ROOT / rel
        previous = p.read_text(encoding="utf-8") if p.exists() else ""
        files[rel] = replace_block(previous, managed)
    drift = []
    for rel, text in files.items():
        p = ROOT / rel
        old = p.read_text(encoding="utf-8") if p.exists() else None
        if old != text:
            drift.append(rel)
            if not args.check:
                write_text(p, text)
    if args.check and drift:
        print("Адаптеры требуют обновления: " + ", ".join(drift), file=sys.stderr)
        return 1
    print("Адаптеры согласованы; файлов: " + str(len(files)))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
