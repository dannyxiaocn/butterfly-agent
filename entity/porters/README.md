# porters

## Purpose

The porter entity is the persistent merge-and-verification persona for this repository. It extends `nutshell_dev_codex` and focuses on validating `ready-` branches, fixing anything they uncover on `wip-` branches, and consolidating porter-system coverage before merge.

## How To Use It

```bash
nutshell new --entity porters
nutshell chat --entity porters "verify ready-foo and merge it if it is clean"
```

## How It Contributes To The Whole System

It provides the repo's dedicated handoff and hardening workflow. Instead of mixing implementation and merge authority in the same persona, `porters` exists to review, validate, and integrate work that is already close to done.

## Notes

- Use this entity for final verification and merge preparation, not for open-ended feature work.
- Active repair work still happens on `wip-<slug>` branches; merge-ready handoff should arrive on `ready-<slug>` branches.
- Its heartbeat is intentionally slower than the default developer entities because porter work is usually batch verification rather than rapid iteration.

## Directory Map

- [prompts/heartbeat.md](/Users/xiaobocheng/agent_core/nutshell/entity/porters/prompts/heartbeat.md)
