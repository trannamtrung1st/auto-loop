"""Guardrails for scenario test coverage."""

from pathlib import Path
import re


SCENARIOS = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ") + [
    "AA",
    "AB",
    "AC",
    "AD",
    "AE",
    "AF",
    "AG",
    "AH",
    "AI",
    "AJ",
    "AK",
]


def test_all_required_scenarios_have_named_tests():
    root = Path(__file__).resolve().parents[1]
    names: set[str] = set()
    pattern = re.compile(r"^def (test_scenario_([A-Z]+)_.*)\(")
    for path in (root / "integration").glob("test_scenarios_*.py"):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            match = pattern.match(line)
            if match:
                names.add(match.group(1))
                letter = match.group(2)
                assert letter in SCENARIOS, f"unexpected scenario id {letter}"
    for letter in SCENARIOS:
        matches = [n for n in names if n.startswith(f"test_scenario_{letter}_")]
        # Avoid A matching AA by requiring the next char after the id to be '_'
        matches = [
            n
            for n in names
            if n.startswith(f"test_scenario_{letter}_")
            and not any(
                n.startswith(f"test_scenario_{other}_")
                for other in SCENARIOS
                if other != letter and other.startswith(letter) and len(other) > len(letter)
            )
        ]
        assert matches, f"no test for scenario {letter}"
    s_tests = [n for n in names if n.startswith("test_scenario_S_")]
    assert len(s_tests) >= 2, "scenario S requires REVISE and BLOCKED branches"
