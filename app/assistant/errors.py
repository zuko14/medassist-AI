"""Kriya Ketu plan errors. Their own module so registry-side code (playbooks) can raise them
without importing policy, which imports the registry."""


class PlanError(ValueError):
    """The plan cannot be offered (e.g. more items than the action allows)."""


class EmptyPlan(ValueError):
    """Nothing to change (e.g. the doctor is already on leave those days). The message says why
    in plain words and is shown to the user, so it never carries exception text."""
