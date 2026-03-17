from __future__ import annotations
import inspect
from abc import ABC, abstractmethod
from typing import Any, Callable, get_type_hints


class BaseTool(ABC):
    """Abstract interface for a tool an agent can call."""

    @abstractmethod
    async def execute(self, **kwargs: Any) -> str:
        """Execute the tool and return a string result."""
        ...

    @abstractmethod
    def to_api_dict(self) -> dict:
        """Return the LLM API representation (Anthropic input_schema format)."""
        ...


def _python_type_to_json_schema(annotation: Any) -> dict:
    """Convert a Python type annotation to a JSON Schema type."""
    if annotation is inspect.Parameter.empty or annotation is None:
        return {"type": "string"}
    origin = getattr(annotation, "__origin__", None)
    if origin is not None:
        # Handle Optional[X] -> {"type": X_type}
        args = getattr(annotation, "__args__", ())
        non_none = [a for a in args if a is not type(None)]
        if non_none:
            return _python_type_to_json_schema(non_none[0])
    mapping = {
        str: {"type": "string"},
        int: {"type": "integer"},
        float: {"type": "number"},
        bool: {"type": "boolean"},
    }
    return mapping.get(annotation, {"type": "string"})


def _build_schema_from_func(func: Callable) -> dict:
    """Build a JSON Schema object from a function's type annotations."""
    sig = inspect.signature(func)
    hints = get_type_hints(func)
    props: dict[str, Any] = {}
    required: list[str] = []
    for name, param in sig.parameters.items():
        if name == "self":
            continue
        props[name] = _python_type_to_json_schema(hints.get(name))
        if param.default is inspect.Parameter.empty:
            required.append(name)
    return {"type": "object", "properties": props, "required": required}


class Tool(BaseTool):
    """An external action that an agent can call."""

    def __init__(
        self,
        name: str,
        description: str,
        func: Callable,
        schema: dict | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self._func = func
        self.schema = schema or _build_schema_from_func(func)

    async def execute(self, **kwargs: Any) -> str:
        result = self._func(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        return str(result)

    def to_api_dict(self) -> dict:
        """Format for LLM API tool definition."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.schema,
        }


def tool(
    name: str | None = None,
    description: str | None = None,
    schema: dict | None = None,
) -> Callable:
    """Decorator to define a Tool from a function.

    Usage:
        @tool(description="Search the web for a query")
        async def search(query: str) -> str:
            return "results..."

        @tool  # also works without parentheses
        def add(a: int, b: int) -> int:
            return a + b
    """
    def decorator(func: Callable) -> Tool:
        _name = name or func.__name__
        _desc = description or (inspect.getdoc(func) or _name)
        return Tool(name=_name, description=_desc, func=func, schema=schema)

    # Support @tool without parentheses
    if callable(name):
        func, name = name, None
        return decorator(func)

    return decorator
