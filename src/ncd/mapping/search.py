from typing import List

from rapidfuzz import fuzz

from .models import MappingRule


class MappingSearch:
    """Enable keyword search for module 4 sections."""

    def __init__(self, rules: dict):
        self.rules = rules

    def search_keywords(self, query: str, limit: int = 10) -> List[MappingRule]:
        """Return the best keyword matches in mapping rules."""

        scored = []

        for rule in self.rules.values():
            best_match = max(
                [fuzz.partial_ratio(query.lower(), kw.lower()) for kw in rule.keywords]
                or [0]
            )
            scored.append((best_match, rule))

        scored.sort(reverse=True, key=lambda x: x[0])
        return [r for score, r in scored[:limit] if score > 60]

    def search_titles(self, query: str, limit: int = 10) -> List[MappingRule]:
        """Search rule titles."""

        scored = []

        for rule in self.rules.values():
            sim = fuzz.partial_ratio(query.lower(), rule.title.lower())
            scored.append((sim, rule))

        scored.sort(reverse=True, key=lambda x: x[0])
        return [r for score, r in scored[:limit] if score > 60]
