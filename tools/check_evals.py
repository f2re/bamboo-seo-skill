#!/usr/bin/env python3
"""Структурная проверка eval-наборов. Модель не запускается."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIRED_ROUTING = {"id", "prompt", "expected_skill", "forbidden_skills"}
REQUIRED_CONTENT = {"id", "prompt", "expected_skill", "expectations"}
SKILLS = {"bamboo-plan", "bamboo-research", "bamboo-content", "bamboo-review",
          "bamboo-publish", "bamboo-analytics"}


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{number}: неверный JSON: {exc.msg}") from exc
        if not isinstance(row, dict):
            raise SystemExit(f"{path}:{number}: ожидается JSON-объект")
        rows.append(row)
    return rows


def check(path: Path, required: set[str], list_field: str) -> int:
    rows = read_jsonl(path)
    ids = set()
    for number, row in enumerate(rows, 1):
        missing = required - row.keys()
        if missing:
            raise SystemExit(f"{path}:{number}: отсутствуют поля {sorted(missing)}")
        if row["id"] in ids:
            raise SystemExit(f"{path}:{number}: повтор id {row['id']}")
        ids.add(row["id"])
        if row["expected_skill"] not in SKILLS:
            raise SystemExit(f"{path}:{number}: неизвестный skill {row['expected_skill']}")
        if not isinstance(row["prompt"], str) or not row["prompt"].strip():
            raise SystemExit(f"{path}:{number}: пустой prompt")
        if not isinstance(row[list_field], list):
            raise SystemExit(f"{path}:{number}: {list_field} должен быть массивом")
        if list_field == "forbidden_skills" and set(row[list_field]) - SKILLS:
            raise SystemExit(f"{path}:{number}: неизвестный forbidden skill")
    return len(rows)


def main() -> int:
    routing = check(ROOT / "evals/routing.jsonl", REQUIRED_ROUTING, "forbidden_skills")
    content = check(ROOT / "evals/content.jsonl", REQUIRED_CONTENT, "expectations")
    print(f"Eval-наборы корректны: routing={routing}, content={content}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
