import pytest
from nutshell.tool import Tool, tool


def test_tool_decorator_basic():
    @tool(description="Add two numbers")
    def add(a: int, b: int) -> int:
        return a + b

    assert isinstance(add, Tool)
    assert add.name == "add"
    assert add.description == "Add two numbers"
    assert add.schema["properties"]["a"] == {"type": "integer"}
    assert add.schema["properties"]["b"] == {"type": "integer"}
    assert add.schema["required"] == ["a", "b"]


def test_tool_decorator_no_parens():
    @tool
    def greet(name: str) -> str:
        return f"Hello, {name}"

    assert isinstance(greet, Tool)
    assert greet.name == "greet"


@pytest.mark.asyncio
async def test_tool_execute_sync():
    @tool(description="Multiply")
    def multiply(a: int, b: int) -> int:
        return a * b

    result = await multiply.execute(a=3, b=4)
    assert result == "12"


@pytest.mark.asyncio
async def test_tool_execute_async():
    @tool(description="Async fetch")
    async def fetch(url: str) -> str:
        return f"content of {url}"

    result = await fetch.execute(url="http://example.com")
    assert result == "content of http://example.com"


def test_tool_to_api_dict():
    @tool(description="Search")
    def search(query: str) -> str:
        return ""

    api = search.to_api_dict()
    assert api["name"] == "search"
    assert api["description"] == "Search"
    assert "input_schema" in api
