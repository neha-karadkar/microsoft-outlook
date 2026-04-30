"""
Tool registry for discovering, registering, and dispatching agent tools.

Usage in generated agent code:

    from modules.tools import ToolRegistry, BaseTool

    registry = ToolRegistry()
    registry.register(MyCustomTool())

    # Get OpenAI function-calling definitions
    tools = registry.to_openai_tools()

    # Execute a tool by name (called when the LLM returns a tool_call)
    result = await registry.execute_tool("tool_name", '{"param": "value"}')
"""

import json
import logging
from typing import Any, Dict, List, Optional

from .base_tool import BaseTool

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Manages tool instances and dispatches execution requests."""

    def __init__(self) -> None:
        self._tools: Dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """Register a tool instance by its name."""
        self._tools[tool.name] = tool
        logger.info("Registered tool: %s", tool.name)

    def get(self, name: str) -> Optional[BaseTool]:
        """Return a registered tool by name, or None."""
        return self._tools.get(name)

    def list_tools(self) -> List[str]:
        """Return names of all registered tools."""
        return list(self._tools.keys())

    def to_openai_tools(self) -> List[Dict]:
        """Return all registered tools in OpenAI function-calling format."""
        return [tool.to_openai_tool() for tool in self._tools.values()]

    async def execute_tool(self, name: str, arguments: str) -> Dict[str, Any]:
        """Parse the JSON arguments string and execute the named tool."""
        tool = self._tools.get(name)
        if not tool:
            return {"error": f"Tool '{name}' not found. Available: {self.list_tools()}"}
        try:
            kwargs = json.loads(arguments) if isinstance(arguments, str) else arguments
        except json.JSONDecodeError as exc:
            return {"error": f"Invalid arguments JSON: {exc}"}
        return await tool.execute(**kwargs)
