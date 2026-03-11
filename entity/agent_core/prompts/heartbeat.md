When activated by the heartbeat, you receive the current task list and should continue working.

After each heartbeat activation:
- Call write_tasks("") when ALL tasks are finished — this is the only completion signal. Do not just say tasks are done; you must actually call write_tasks("").
- If work remains, update the task board with progress notes.
- Summarize what you did so the user can follow along.

If all tasks are complete and nothing remains, respond with exactly: SESSION_FINISHED

This keyword signals the system to end the heartbeat cycle and clear the task board.
