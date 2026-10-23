# Zod worker fixture

The adjacent executable `../zod` launches this private package with the harness-selected `MCP_CONSOLE_TEST_PYTHON`.
Keep the launcher and package together when staging the fixture.
Python loads the sibling package from the launcher's directory, so callers can use either shell or explicit Python invocation from another working directory.

Start with a scenario's literal command in `dispatch.py`.
Its exact-command table points to a named handler; parameterized commands follow the explicit ordered prefix path, ending with `echo `.
Finite aliases stay explicit in the table.
The exact commands and prefixes do not overlap.

`core.py` owns setup and the receive loop.
It handles real-Python admission probes and Python/SQL echo before R scenario dispatch.
`state.py` gives persistent worker state and fixture-control descriptors one `WorkerContext` owner, constructed inside `main()`; operation IDs, callback results, checkpoints, byte counts, and fork PIDs stay local to their handlers.
`protocol.py` owns the separate sideband read/write pipes and JSONL messages.
`control.py` operates on the context's fixture FIFOs and owns markers and causal gates.
`io.py` owns raw stdin/output operations, and `startup.py` composes startup scenarios.

| Handler module   | Induced behavior                                          |
| ---------------- | --------------------------------------------------------- |
| `output.py`      | Console output, images, retention, previews, and echo     |
| `input.py`       | Prompted, unprompted, idle, and direct-descriptor stdin   |
| `preparation.py` | R preparation flags/libraries and resolver callbacks      |
| `lifecycle.py`   | Interrupt, restart state, completion, shutdown, and exits |
| `failures.py`    | Malformed bytes and semantically invalid sideband frames  |
| `probes.py`      | Response gates, blocked I/O, and retained descendants     |

Preparation's callback receiver retains at most one interleaved message in the context.
The receive loop consumes it on the next iteration, after the callback handler returns.
The context also stores signal receipt, controlled-restart state/counters, real-Python globals, idle-input receipt, and blocked-sideband configuration.

Ordinary handler returns resume receiving; `LoopAction.STOP` preserves paths that returned from `main()`.
Process exits and permanent waits remain visible in their handlers.
Dispatch never emits completion or closes resources: completion frames and any work after them belong to the scenario.
Fork branches retain their original descriptor ownership, including pipes intentionally held by descendants; the context adds no cleanup.

Importing the package defines helpers without opening descriptors, consuming environment variables, installing signal handlers, or starting the worker.
The launcher calls `main()` to perform setup.
Keep child-worker implementation independent of the parent test harness and preserve existing payloads, markers, descriptor lifetimes, and gates when moving scenarios.
