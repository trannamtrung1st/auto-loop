"""Guardrails for §44 scenario test coverage."""

from pathlib import Path


def test_all_scenarios_a_t_have_named_tests():
    root = Path(__file__).resolve().parents[1]
    names: set[str] = set()
    for path in (root / "integration").glob("test_scenarios_*.py"):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("def test_scenario_"):
                names.add(line.split("(")[0].replace("def ", ""))
    for letter in "ABCDEFGHIJKLMNOPQRST":
        matches = [n for n in names if n.startswith(f"test_scenario_{letter}_")]
        assert matches, f"no test for scenario {letter}"
    s_tests = [n for n in names if n.startswith("test_scenario_S_")]
    assert len(s_tests) >= 2, "scenario S requires REVISE and BLOCKED branches"
