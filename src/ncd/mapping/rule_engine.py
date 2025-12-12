from typing import Any, Dict, List

from rapidfuzz import fuzz

from .models import MappingResult, MappingRule


class CTDMappingEngine:
    """Full rule-based engine with weighted scoring."""

    def __init__(self, rules: Dict[str, MappingRule]):
        self.rules = rules

    # ------------------------------
    # Direct mapping (exact section)
    # ------------------------------
    def map_section(self, module4_section: str) -> MappingResult:
        rule = self.rules.get(module4_section)
        if not rule:
            return MappingResult(
                ctd_sections=[],
                score_breakdown={},
                matched_keywords=[],
                module4_section=module4_section,
            )

        score = {ctd: 1.0 for ctd in rule.maps_to}
        return MappingResult(
            ctd_sections=rule.maps_to,
            score_breakdown=score,
            matched_keywords=rule.keywords,
            module4_section=module4_section,
        )

    # --------------------------------------------
    # Keyword → CTD inference (no explicit section)
    # --------------------------------------------
    def infer_from_text(self, text: str) -> List[MappingResult]:
        """Find best matching module4 sections based on text content."""

        results = []

        for rule in self.rules.values():
            matched = []
            score_total = 0.0

            for kw in rule.keywords:
                sim = fuzz.partial_ratio(kw.lower(), text.lower())
                if sim > 70:  # threshold
                    matched.append(kw)
                    score_total += sim / 100

            if matched:
                results.append(
                    MappingResult(
                        ctd_sections=rule.maps_to,
                        score_breakdown={s: score_total for s in rule.maps_to},
                        matched_keywords=matched,
                        module4_section=rule.module4_section,
                    )
                )

        # Sort by descending score
        results.sort(key=lambda r: sum(r.score_breakdown.values()), reverse=True)
        return results

    # --------------------------------------------
    # Validate coverage (sanity)
    # --------------------------------------------
    def validate_rules(self) -> Dict[str, Any]:
        """Check that the mapping table is complete."""

        missing_title = []
        missing_keywords = []
        missing_targets = []

        for sec, rule in self.rules.items():
            if not rule.title:
                missing_title.append(sec)
            if not rule.keywords:
                missing_keywords.append(sec)
            if not rule.maps_to:
                missing_targets.append(sec)

        return {
            "missing_title": missing_title,
            "missing_keywords": missing_keywords,
            "missing_targets": missing_targets,
        }
