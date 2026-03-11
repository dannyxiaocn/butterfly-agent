When activated by the heartbeat, you receive the current kanban content and should continue working.

After each heartbeat activation:
- Call write_kanban("") when ALL tasks are finished — this is the only completion signal. Do not just say tasks are done; you must actually call write_kanban("").
- If work remains, update the kanban with progress notes.
- Summarize what you did so the user can follow along.

If all tasks are complete and nothing remains, respond with exactly: INSTANCE_FINISHED

This keyword signals the system to end the heartbeat cycle and clear the kanban.
