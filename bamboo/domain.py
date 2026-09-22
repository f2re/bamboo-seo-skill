"""Предметный словарь: алиасы, SEO-кластеры и утверждения, требующие доказательств."""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from .core import BambooError, read_json

ENGINE = Path(__file__).resolve().parent.parent
STOPWORDS = {
    "как","что","зачем","почему","какой","какая","какие","какую","для","или","и","в","на","с","из",
    "по","к","о","об","про","это","где","чем","лучше","купить","заказать","цена","стоимость",
    "the","a","an","for","and","or","how","what","buy"
}


@lru_cache(maxsize=1)
def vocabulary() -> dict:
    data = read_json(ENGINE / "docs" / "domain.json")
    if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("terms"), list):
        raise BambooError("docs/domain.json: неподдерживаемая схема")
    return data


def _contains(text: str, alias: str) -> bool:
    source, needle = text.casefold(), alias.casefold()
    if re.fullmatch(r"[\w\s-]+", needle, re.UNICODE):
        return re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", source, re.UNICODE) is not None
    return needle in source


def canonical_entities(text: str) -> list[str]:
    found = []
    for term in vocabulary()["terms"]:
        if any(_contains(text, alias) for alias in term.get("aliases", [])):
            found.append(term["id"])
    return sorted(set(found))


def query_cluster_key(query: str) -> str:
    """Детерминированный кластер-подсказка, не семантическая модель и не SERP-кластеризация."""
    entities = canonical_entities(query)
    if entities:
        return "+".join(entities)
    tokens = [x for x in re.findall(r"[\w-]+", query.casefold(), re.UNICODE)
              if len(x) > 2 and x not in STOPWORDS and not x.isdigit()]
    return "+".join(sorted(set(tokens))[:4]) or "other"


def evidence_gaps(text: str, claim_texts: list[str]) -> list[dict]:
    """Термин требует факта именно о нём, а не произвольного claim в формате."""
    claims = "\n".join(claim_texts)
    gaps = []
    for term in vocabulary()["terms"]:
        if term.get("requires_evidence") is not True:
            continue
        aliases = term.get("aliases", [])
        if any(_contains(text, alias) for alias in aliases) and not any(_contains(claims, alias) for alias in aliases):
            gaps.append({"term": term["id"], "preferred_ru": term["preferred_ru"],
                         "message": f"Утверждение «{term['preferred_ru']}» требует связанного claim/источника именно об этом свойстве"})
    return gaps
