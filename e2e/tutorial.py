"""Exact S2 tutorial command sequence.

Derived from the frontend sources (vendor/frontend/web/src/lib):
  * content/tutorial.ts   TUTORIAL_SPOTS / TUTORIAL_LINKS / TUTORIAL_PIPE (row 5 of the 24x14 starting hangar)
  * sim/templates.ts      tutorialLayout() - the reference layout; verified: coordinates below == tutorialLayout()
  * content/dialogs.ts    QUESTS q_intro -> q_brew -> q_bottles -> q_filler -> q_line -> q_dock (ids used by dialogDone)
  * ui/Intro.svelte, ui/AgeGate.svelte, dialog/quests.svelte.ts   which command each UI step sends
Quest ids are sent with `dialogDone` when the quest's steps are done (q_dock only after the order is shipped).
"""

from typing import Any

Cmd = dict[str, Any]

ROW = 5  # TUTORIAL_ROW: gates W/5 and E/5
TUTORIAL_ORDER_ID = "tutorial_first"  # content/orders.ts tutorialOrder()


def _place(kind: str, x: int, y: int, rot: int = 0) -> Cmd:
    return {"type": "place", "kind": kind, "x": x, "y": y, "rot": rot}


def _belt(xs: list[int]) -> Cmd:
    return {"type": "placeConveyor", "kind": "belt", "path": [{"x": x, "y": ROW, "dir": "E"} for x in xs]}


# (label, command) in the order the quests ask for it.
INTRO: list[tuple[str, Cmd]] = [
    ("age", {"type": "ageConfirm"}),
    ("name", {"type": "renameFactory", "name": "E2E Kvas"}),
    ("q_intro", {"type": "dialogDone", "id": "q_intro"}),
]

BUILD: list[tuple[str, Cmd]] = [
    # q_brew
    ("brew_tank", _place("brew_tank", 6, 1, 1)),
    ("q_brew", {"type": "dialogDone", "id": "q_brew"}),
    # q_bottles
    ("intake", _place("intake", 0, ROW)),
    ("blow", _place("blow", 3, ROW - 1)),
    ("belt intake->blow", _belt([2])),
    ("q_bottles", {"type": "dialogDone", "id": "q_bottles"}),
    # q_filler
    ("filler", _place("filler", 6, ROW - 1)),
    ("belt blow->filler", _belt([5])),
    ("pipe tank->filler", {"type": "placeConveyor", "kind": "pipe", "path": [{"x": 6, "y": ROW - 2, "dir": "S"}]}),
    ("q_filler", {"type": "dialogDone", "id": "q_filler"}),
    # q_line
    ("capper", _place("capper", 9, ROW)),
    ("belt filler->capper", _belt([8])),
    ("labeler", _place("labeler", 12, ROW)),
    ("belt capper->labeler", _belt([11])),
    ("packer", _place("packer", 17, ROW - 1)),
    ("belt labeler->packer", _belt([14, 15, 16])),
    ("q_line", {"type": "dialogDone", "id": "q_line"}),
    # q_dock (first step: the dock at the E gate + belt from the packer)
    ("dock", _place("dock", 22, ROW)),
    ("belt packer->dock", _belt([19, 20, 21])),
]

# Expected end state of BUILD (== tutorialLayout().layout): kind -> (x, y, rot)
EXPECTED_BUILDINGS = {
    "brew_tank": (6, 1, 1),
    "intake": (0, 5, 0),
    "blow": (3, 4, 0),
    "filler": (6, 4, 0),
    "capper": (9, 5, 0),
    "labeler": (12, 5, 0),
    "packer": (17, 4, 0),
    "dock": (22, 5, 0),
}
EXPECTED_BELT_XS = [2, 5, 8, 11, 14, 15, 16, 19, 20, 21]  # all dir E on row 5
EXPECTED_PIPES = [(6, 3, "S")]
