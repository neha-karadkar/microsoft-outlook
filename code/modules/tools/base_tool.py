"""
Abstract base class for agent tools.

All tools used with the ToolRegistry must extend BaseTool and implement
the three abstract properties (name, description, parameters_schema) and
the execute() async method.
"""

import abc
from typing import Any, Dict


class BaseTool(abc.ABC):
    """Base class that every agent tool must inherit from."""

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Unique tool name used in OpenAI function-calling."""

    @property
    @abc.abstractmethod
    def description(self) -> str:
        """Human-readable description sent to the LLM."""

    @property
    @abc.abstractmethod
    def parameters_schema(self) -> Dict:
        """JSON Schema object describing the tool's accepted parameters."""

    @abc.abstractmethod
    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Run the tool with the given keyword arguments and return a result dict."""

    def to_openai_tool(self) -> Dict:
        """Convert this tool to the OpenAI function-calling tool format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema,
            },
        }
