import json
from pathlib import Path
from typing import Dict

from .models import MappingRule


class MappingLoader:
    """Loads module4_to_26 mapping JSON."""

    def __init__(self, json_path: str):
        self.json_path = Path(json_path)
        self.rules: Dict[str, MappingRule] = {}

    def load(self) -> Dict[str, MappingRule]:
        """Load JSON mapping file and convert to rule models."""
        raw = json.loads(self.json_path.read_text())

        for section_id, entry in raw.items():
            rule = MappingRule(
                module4_section=section_id,
                title=entry.get("title", ""),
                keywords=entry.get("keywords", []),
                maps_to=entry.get("maps_to", []),
                category=entry.get("category", None),
            )
            self.rules[section_id] = rule

        return self.rules

    def get_rule(self, module4_section: str) -> MappingRule:
        """Fetch rule by module 4 section."""
        if module4_section in self.rules:
            return self.rules[module4_section]
        raise KeyError(f"No mapping rule found for section: {module4_section}")
