"""Operator knobs: temperament and the stress threshold."""

import unittest

from digital_life import domain


class ConfigKnobTests(unittest.TestCase):
    def tearDown(self):
        domain.configure()

    def test_temperament_knobs_are_clamped_into_new_states(self):
        domain.configure(temperament={"sensitivity": 9.0, "resilience": 0.01, "enthusiasm": 5, "metabolism": "x"})
        state = domain.default_state(1_700_000_000_000.0)
        self.assertEqual(state["temperament"]["sensitivity"], 2.0)
        self.assertEqual(state["temperament"]["resilience"], 0.5)
        self.assertEqual(state["temperament"]["enthusiasm"], 20.0)
        self.assertEqual(state["temperament"]["metabolism"], 1.0)

    def test_stress_threshold_moves_the_directive_boundary(self):
        domain.configure(stress_cortisol=30)
        self.assertTrue(domain.stress_directive(35.0))
        domain.configure(stress_cortisol=95)
        self.assertEqual(domain.stress_directive(35.0), "")

    def test_reset_restores_the_defaults(self):
        domain.configure(temperament={"sensitivity": 2.0}, stress_cortisol=30)
        domain.configure()
        self.assertEqual(domain.stress_threshold(), 80.0)
        state = domain.default_state(1_700_000_000_000.0)
        self.assertEqual(state["temperament"]["sensitivity"], 1.0)


if __name__ == "__main__":
    unittest.main()
