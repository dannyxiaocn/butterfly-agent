# `entity/agent/tools`

This directory defines the default tool schemas exposed by the base entity.

## Files

- `bash.json`: shell execution.
- `skill.json`: load a skill body on demand.
- `web_search.json`: web search through the configured backend.

## How To Use This Part

Edit the JSON here when changing the description or schema of a default tool. Add the tool to `agent.yaml` only if new sessions should receive it by default.

## How It Contributes To The Whole System

These files are the configuration surface that makes built-in runtime tools available to ordinary sessions.

