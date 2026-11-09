# Glossary

<span id="user-terms"></span>

**Agent:** The model-driven program that submits code and uses its results.

**Client:** The application that connects to Console, such as a coding assistant or a program using a Console adapter.

**MCP:** Model Context Protocol, the interface through which the client discovers and calls Console's tool.

**Console process:** One server lifetime, from startup to shutdown.
It manages the connection's sessions and recording.

**Session:** A named workspace with its own R, Python, and SQL state and package requirements.
The initial session is named `default`; additional named sessions can run concurrently.
Objects remain available between cells until the session's worker is restarted or the connection closes.
Restarting replaces the worker and clears its live state while retaining the session's accepted requirements.

**Worker session:** One lifetime of the runtime process backing a named session.
Recordings number worker sessions separately, so a restart gets a new number even when the public session name stays the same.

**Cell:** One complete block of R, Python, or SQL code submitted for execution.
Interactive input is supplied separately.

**REPL:** Read–evaluate–print loop: run code, inspect the result, and continue in the same session.

**Execution host:** The machine or environment where Console runs and stores its files.
Console and the client's shell and filesystem tools should use the same host and project.

**Managed environment:** An environment whose packages Console prepares for the session.

**Existing environment:** A Python installation or virtual environment selected by the user.
Console uses its installed packages rather than preparing them.

**Preparation:** Making dependencies available.
Code still needs to import a Python module, attach an R package, or load an extension when appropriate.

**Requirements:** The requested dependencies.
They are not an inventory of installed packages or a lockfile.
Accepted requirements survive session restarts within the same Console process.

**Recording:** Local files containing calls, source, input, output, and artifacts.
A recording belongs to one Console process and can include multiple sessions.
Recordings are unredacted and do not preserve live state.

**Capture:** The authoritative retained events, text, and images in a recording.

**Transcript:** The human-readable account derived from capture.

**Sandbox:** Operating-system restrictions on the code Console runs.
Default permissions restrict writes and direct networking but allow host-file reads, including readable secrets.

For polling and other client operations, see [Drive send directly](SEND_OPERATIONS.md).
<span id="developer-terms"></span>

Internal terms such as relay, generation, and retirement are defined in [Architecture](ARCHITECTURE.md#developer-terms).
