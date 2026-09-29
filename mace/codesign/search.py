"""mace.codesign.search -- the strategies that pick the next designs to try.

Every strategy answers ``propose(history, n)`` with up to *n* designs from
its space that the search has not evaluated yet, and hears each round's
outcome through ``observe(evaluations)``. The search loop (see
:mod:`mace.codesign.run`) gives each the same space, the same number of
simulations, and the same batches, so a comparison differs only in how
each chooses.

- :class:`LLMProposer`: MACE. The LLM sees the objective, the space, the
  area budget, and every earlier design with its outcome.
- :class:`RandomSearch`: uniform samples, seeded.
- :class:`GridSearch`: a fixed list of designs, in order.
- :class:`BayesianSearch`: Optuna's TPE sampler through its ask-and-tell
  interface. Optuna is an optional dependency (the ``eval`` extra).
"""

from __future__ import annotations

import random
import re

from mace import usage
from mace.codesign.space import ARIANE_WAY_RULE, Design, DesignSpace, parse_design
from mace.planner import render_inputs
from mace.spec import MaceSpec


class RandomSearch:
    name = "random"

    def __init__(self, space: DesignSpace, seed: int = 0):
        self.space = space
        self.rng = random.Random(seed)

    def propose(self, history: list, n: int) -> list[Design]:
        seen = {e.design for e in history}
        picked: list[Design] = []
        for _ in range(200 * max(n, 1)):
            if len(picked) == n or len(seen) + len(picked) >= self.space.count():
                break
            design = self.space.sample(self.rng)
            if design not in seen and design not in picked:
                picked.append(design)
        return picked

    def observe(self, evaluations: list) -> None:
        pass


class GridSearch:
    name = "grid"

    def __init__(self, designs: list[Design]):
        self.designs = list(designs)

    def propose(self, history: list, n: int) -> list[Design]:
        seen = {e.design for e in history}
        return [d for d in self.designs if d not in seen][:n]

    def observe(self, evaluations: list) -> None:
        pass


class BayesianSearch:
    """Optuna's TPE sampler; a design that fails the check, or breaks the
    area budget, scores ten times the slowest feasible finish seen."""

    name = "bayesian"

    def __init__(self, space: DesignSpace, seed: int = 0):
        import optuna

        optuna.logging.set_verbosity(optuna.logging.WARNING)
        self.space = space
        self.study = optuna.create_study(
            direction="minimize", sampler=optuna.samplers.TPESampler(seed=seed, constant_liar=True)
        )
        self.pending: dict[Design, object] = {}
        self.worst = 0

    def propose(self, history: list, n: int) -> list[Design]:
        seen = {e.design for e in history}
        picked: list[Design] = []
        for _ in range(20 * max(n, 1)):
            if len(picked) == n:
                break
            trial = self.study.ask()
            choices = {knob: trial.suggest_categorical(knob, list(values)) for knob, values in self.space.knobs.items()}
            design = self.space.design(choices)
            if design in seen or design in picked:
                # Already evaluated: tell the sampler what it scored, so it
                # moves on instead of asking again.
                self.study.tell(trial, self._score_of(design, history))
                continue
            self.pending[design] = trial
            picked.append(design)
        return picked

    def _score_of(self, design: Design, history: list) -> float:
        for e in history:
            if e.design == design:
                return self._score(e)
        return self._penalty()

    def _penalty(self) -> float:
        return 10.0 * self.worst if self.worst else 1e12

    def _score(self, evaluation) -> float:
        return float(evaluation.sim_time) if evaluation.feasible else self._penalty()

    def observe(self, evaluations: list) -> None:
        for e in evaluations:
            if e.feasible and e.sim_time is not None:
                self.worst = max(self.worst, e.sim_time)
        for e in evaluations:
            trial = self.pending.pop(e.design, None)
            if trial is not None:
                self.study.tell(trial, self._score(e))


_PROPOSER_HEADER = """\
You are searching for the OpenPiton/{core} design that finishes the gate
workloads soonest in simulation, within a total cache area budget. A design
counts only if every tile passes and its cache area fits the budget.

"""

_PROPOSER_RULES = """\
Design space:
{space}

Total cache area budget: {budget} square microns over all tiles.
{area_notes}{way_rule}
Propose {n} new designs, one per line, in exactly this format (a footer, not
prose), using only values from the space above:

DESIGN: l15=<size>,<assoc> l1d=<size>,<assoc> l1i=<size>,<assoc> l2=<size>,<assoc> network=<network>

Do not repeat a design already tried. Nothing else you write is parsed, but
keep the rest brief.
"""

_DESIGN_LINE = re.compile(r"(?im)^\s*\**\s*DESIGN:\s*(.+?)\s*$")


class LLMProposer:
    """MACE's proposer: one LLM call per round, recorded under ``propose``."""

    name = "mace"
    phase = "propose"

    def __init__(
        self, llm, spec: MaceSpec, space: DesignSpace, area_budget_um2: float | None, area_notes: str = ""
    ):
        self.llm = llm
        self.spec = spec
        self.space = space
        self.area_budget_um2 = area_budget_um2
        # Each cache geometry's area (see mace.codesign.area.area_notes); a
        # design over budget is rejected before it is built.
        self.area_notes = area_notes

    def build_prompt(self, history: list, n: int) -> str:
        budget = "none" if self.area_budget_um2 is None else f"{self.area_budget_um2:.0f}"
        prompt = (
            _PROPOSER_HEADER.format(core=self.spec.core)
            + render_inputs(self.spec)
            + "\n"
            + _PROPOSER_RULES.format(
                space=self.space.describe(), budget=budget, n=n,
                area_notes=(
                    "Cache area in square microns over all tiles, per cache and geometry; a design's area is "
                    "the sum over its four caches, and a design over budget is rejected without a simulation:\n"
                    f"{self.area_notes}\n"
                    if self.area_notes else ""
                ),
                way_rule=(
                    f"{ARIANE_WAY_RULE} A design that breaks this is rejected without a simulation.\n"
                    if self.spec.core == "ariane" else ""
                ),
            )
        )
        if history:
            rows = []
            for e in history:
                if e.rejected:
                    rows.append(f"- {e.design.describe()}: rejected before building ({e.rejected}), "
                                f"area {e.area_um2:.0f}")
                    continue
                outcome = "passed" if e.passed else "failed the check"
                fits = "fits" if (self.area_budget_um2 is None or e.area_um2 <= self.area_budget_um2) else "over budget"
                finish = e.sim_time if e.sim_time is not None else "-"
                rows.append(f"- {e.design.describe()}: {outcome}, finish time {finish}, area {e.area_um2:.0f} ({fits})")
            prompt += "\nDesigns tried so far:\n" + "\n".join(rows) + "\n"
        return prompt

    def propose(self, history: list, n: int) -> list[Design]:
        query = usage.prompt(self.llm, self.phase, self.build_prompt(history, n))
        seen = {e.design for e in history}
        picked: list[Design] = []
        for line in _DESIGN_LINE.findall(query.result):
            design = parse_design(line)
            if design is None or not self.space.contains(design) or design in seen or design in picked:
                continue
            picked.append(design)
        return picked[:n]

    def observe(self, evaluations: list) -> None:
        pass
