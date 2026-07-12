"""Context-window budgeting independent from a concrete model provider."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """Effective input budget after non-history and output reservations."""

    model_window: int
    reserved_output: int = 8_192
    fixed_prompt_tokens: int = 0
    tool_schema_tokens: int = 0
    safety_margin: int = 2_048

    @property
    def available_input(self) -> int:
        reserved = (
            self.reserved_output
            + self.fixed_prompt_tokens
            + self.tool_schema_tokens
            + self.safety_margin
        )
        return max(1_024, self.model_window - reserved)

    @property
    def request_input_limit(self) -> int:
        """Provider prompt capacity; actual usage already includes fixed prompt/tools."""
        return max(
            1_024,
            self.model_window - self.reserved_output - self.safety_margin,
        )

    def threshold(self, fraction: float) -> int:
        return max(1, int(self.available_input * fraction))
