#!/usr/bin/env python3
"""Nutshell interactive CLI chat.

Usage:
    python chat.py
    python chat.py --model claude-haiku-4-5-20251001
    python chat.py --system "You are a pirate. Speak only in pirate."
    python chat.py --api-key sk-ant-...

Commands during chat:
    /clear      Clear conversation history
    /system     Print current system prompt
    /system <p> Change system prompt to <p>
    /tools      List loaded tools
    /exit       Exit
"""
import argparse
import asyncio
import os
import sys


def parse_args():
    parser = argparse.ArgumentParser(
        description="Nutshell interactive CLI chat",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--model", "-m",
        default="claude-haiku-4-5-20251001",
        help="Model ID (default: claude-haiku-4-5-20251001)",
    )
    parser.add_argument(
        "--system", "-s",
        default="You are a helpful assistant.",
        help="System prompt",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("ANTHROPIC_API_KEY"),
        help="Anthropic API key (default: ANTHROPIC_API_KEY env var)",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colored output",
    )
    return parser.parse_args()


# ANSI color helpers
def _supports_color(no_color: bool) -> bool:
    return not no_color and sys.stdout.isatty()


class Colors:
    def __init__(self, enabled: bool):
        self.e = enabled

    def _c(self, code, text):
        return f"\033[{code}m{text}\033[0m" if self.e else text

    def bold(self, t):    return self._c("1", t)
    def dim(self, t):     return self._c("2", t)
    def cyan(self, t):    return self._c("36", t)
    def green(self, t):   return self._c("32", t)
    def yellow(self, t):  return self._c("33", t)
    def red(self, t):     return self._c("31", t)
    def magenta(self, t): return self._c("35", t)


def print_banner(c: Colors, model: str, system: str):
    print(c.bold(c.cyan("╭─────────────────────────────────────────╮")))
    print(c.bold(c.cyan("│          nutshell  chat  cli             │")))
    print(c.bold(c.cyan("╰─────────────────────────────────────────╯")))
    print(c.dim(f"  model   : {model}"))
    preview = system if len(system) <= 60 else system[:57] + "..."
    print(c.dim(f"  system  : {preview}"))
    print(c.dim("  /clear /system /tools /exit for commands"))
    print()


async def chat_loop(args):
    from nutshell import Agent, AnthropicProvider

    if not args.api_key:
        print("Error: no API key. Set ANTHROPIC_API_KEY or use --api-key.", file=sys.stderr)
        sys.exit(1)

    c = Colors(_supports_color(args.no_color))
    print_banner(c, args.model, args.system)

    provider = AnthropicProvider(api_key=args.api_key)
    agent = Agent(
        system_prompt=args.system,
        model=args.model,
        provider=provider,
        release_policy="persistent",
    )

    while True:
        # Prompt
        try:
            user_input = input(c.bold(c.green("you  ❯ "))).strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{c.dim('Bye!')}")
            break

        if not user_input:
            continue

        # Built-in commands
        if user_input.lower() in ("/exit", "/quit", "/q"):
            print(c.dim("Bye!"))
            break

        if user_input.lower() == "/clear":
            agent.close()
            print(c.yellow("  History cleared."))
            continue

        if user_input.lower() == "/tools":
            if agent.tools:
                print(c.yellow(f"  Tools: {', '.join(t.name for t in agent.tools)}"))
            else:
                print(c.yellow("  No tools loaded."))
            continue

        if user_input.lower() == "/system":
            print(c.yellow(f"  System: {agent.system_prompt}"))
            continue

        if user_input.lower().startswith("/system "):
            new_prompt = user_input[8:].strip()
            agent.system_prompt = new_prompt
            agent.close()  # clear history when system prompt changes
            print(c.yellow(f"  System prompt updated. History cleared."))
            continue

        if user_input.startswith("/"):
            print(c.red(f"  Unknown command: {user_input}"))
            continue

        # Run agent
        try:
            print(c.dim("  thinking..."), end="\r")
            result = await agent.run(user_input)
            # Clear "thinking..." line
            print(" " * 20, end="\r")

            # Print tool calls (if any)
            for tc in result.tool_calls:
                print(c.magenta(f"  [tool] {tc.name}({tc.input})"))

            # Print response
            print(c.bold(c.cyan("agent❯ ")) + result.content)
            print()

        except KeyboardInterrupt:
            print(f"\n{c.yellow('  Interrupted.')}")
            continue
        except Exception as exc:
            print(c.red(f"  Error: {exc}"))
            continue


def main():
    args = parse_args()
    asyncio.run(chat_loop(args))


if __name__ == "__main__":
    main()
