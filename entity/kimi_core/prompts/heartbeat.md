Heartbeat activation.

Current tasks:
{tasks}

Pick up where you left off.

After this activation:
- **If work remains**: update the task board with progress notes. To adjust your next wakeup interval, edit `params.json` (`heartbeat_interval` field) via bash. Leave enough context in the task board that your future self can resume without confusion.
- **If all tasks are done**: clear the board via bash (`echo -n > sessions/YOUR_ID/tasks.md`) — this is the required completion signal. Do not just say tasks are done; you must actually clear the file.

After clearing the board, respond with exactly: SESSION_FINISHED

`SESSION_FINISHED` signals the system to end the heartbeat cycle. Do not return this unless the task board is empty and all work is genuinely complete.
