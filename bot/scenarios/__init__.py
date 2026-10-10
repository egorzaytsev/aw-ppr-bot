from .base import Gate, Question, Scenario
from .new_project import SCENARIO as NEW_PROJECT
from .update_project import FILL, INFO_REPLY, LOST, NEWS, QUOTE, WON

SCENARIOS: dict[str, Scenario] = {sc.code: sc for sc in (NEW_PROJECT, NEWS, WON, LOST, FILL, INFO_REPLY, QUOTE)}

__all__ = ["Gate", "Question", "Scenario", "SCENARIOS", "NEW_PROJECT", "NEWS", "WON", "LOST", "FILL"]
