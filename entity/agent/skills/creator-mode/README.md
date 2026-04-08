# entity/agent/skills/creator-mode

This directory contains the `creator-mode` skill.

## What It Is

`SKILL.md` teaches an agent how to create or modify tools and skills inside its own session and then hot-reload them with `reload_capabilities`.

## How To Use It

Expose this skill whenever you want agents to self-extend at runtime rather than waiting for repository-level changes.

## How It Fits

It is the base entity's meta-capability skill: it explains how agents add new capabilities to themselves inside `core/tools/` and `core/skills/`.
