"""Base class for evaluation tasks."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class BaseTask(ABC):
    """Abstract base class for evaluation tasks."""

    index: int
    description: str

    @abstractmethod
    def get_prompt(self) -> str:
        """Get the prompt to send to the agent."""
        ...

    @abstractmethod
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        ...
