"""The line at the top of the console is a claim, so it is a tested one.

"3 need you, 17 handled" is what a judge reads first. It is counted from the
board's own loop states, in code, and this is what stops the emoji beside a
patient and the number in the header from ever disagreeing.
"""

from __future__ import annotations

import unittest

from core import board
from core.models import LoopState


class Colours(unittest.TestCase):
    def test_every_loop_state_has_a_colour(self) -> None:
        for state in LoopState.__args__:  # type: ignore[attr-defined]
            with self.subTest(state=state):
                self.assertIn(state, board.COLOUR_FOR)

    def test_an_unknown_state_counts_as_in_flight_not_as_done(self) -> None:
        self.assertEqual(board.colour("something_new"), "yellow")
        self.assertNotEqual(board.colour("something_new"), "green")


class Tally(unittest.TestCase):
    def test_the_two_states_that_need_a_human(self) -> None:
        counts = board.tally(["pending_review", "received", "unreachable"])
        self.assertEqual(counts["need_you"], 3)
        self.assertEqual(counts["handled"], 0)

    def test_what_sanad_is_carrying_is_handled(self) -> None:
        counts = board.tally(["open", "waiting_patient", "done"])
        self.assertEqual(counts["need_you"], 0)
        self.assertEqual(counts["handled"], 3)

    def test_the_line_reads_the_way_the_pitch_says_it(self) -> None:
        counts = board.tally(["pending_review"] * 3 + ["waiting_patient"] * 17)
        self.assertEqual(counts["line"], "3 need you, 17 handled")

    def test_an_empty_board_says_zero_and_does_not_divide(self) -> None:
        counts = board.tally([])
        self.assertEqual(counts["total"], 0)
        self.assertEqual(counts["line"], "0 need you, 0 handled")

    def test_the_counts_add_up_to_the_total(self) -> None:
        states = ["open", "done", "pending_review", "unreachable", "waiting_patient"]
        counts = board.tally(states)
        four = counts["red"] + counts["yellow"] + counts["green"] + counts["white"]
        self.assertEqual(four, counts["total"], len(states))


if __name__ == "__main__":
    unittest.main()
