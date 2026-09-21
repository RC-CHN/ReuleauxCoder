# Execution shell selection

Use `/shell` to open the TUI picker or list available shells in the CLI. Each
entry shows its name, executable path and environment. The current choice is
marked; **Automatic** restores the runtime's default selection.

```text
/shell
/shell use pwsh
/shell use "C:\Program Files\PowerShell\7\pwsh.exe"
/shell auto
```

`use` accepts a discovered entry's ID, exact path, or unambiguous name. When
multiple installations share a name, choose the exact menu entry or ID instead.
Reopen `/shell` (or use `/shell refresh`) after installing or removing a shell.

The preference belongs to the current runtime and is not written to user,
workspace or operating-system configuration. Restarting returns to automatic
selection. Switching affects new commands, including TTY commands; running
processes keep their original shell. Commands submitted during an agent turn
are queued until the turn finishes. Newly scoped child backends inherit a
snapshot; later parent changes do not change the child's preference.

The model receives the selected shell's name, path and environment in its
execution context and shell tool description. Commands are passed as supplied;
the runtime does not translate Bash syntax into PowerShell or vice versa. If
an explicitly selected executable disappears, execution fails instead of
silently falling back to another shell.

## Windows and WSL

On Windows, `/shell` lists native shells and installed WSL distributions. Choose
a distribution, then choose one of the shells discovered inside it. The CLI
equivalent is:

```text
/shell wsl Ubuntu
/shell use <ID shown in the list>
```

Distribution names may contain spaces. Opening the main list runs only
`wsl.exe --list --quiet`; it does not launch shell probes in every distribution.
Opening a distribution starts that distribution if necessary and probes it
with `/bin/sh`. Listing has a three-second timeout; the selected distribution's
probe has an eight-second timeout. WSL listing failures leave native choices
available and display a diagnostic. Selecting a listed entry does not repeat
the discovery probe.

Execution uses `wsl.exe --distribution <name> --cd <cwd> --exec <shell> ...`.
The workspace directory remains a Windows path passed to WSL for translation.
For `\\wsl$\Ubuntu\...` or `\\wsl.localhost\Ubuntu\...` workspaces, the runtime
passes the Linux path within the matching distribution. A workspace UNC path
for a different distribution is rejected. File paths embedded in command text
must already use the selected shell's conventions.

Native Windows discovery includes Git Bash, PowerShell, Windows PowerShell and
Command Prompt when available. The legacy `System32\bash.exe` WSL launcher is
excluded from native Bash discovery and automatic preference. WSL inside Linux
is treated as its current Linux environment and labeled with its distribution.
The WSL command interface follows Microsoft's
[basic WSL commands](https://learn.microsoft.com/en-us/windows/wsl/basic-commands).

## Discovery and scope

Linux/macOS discovery checks `PATH`, `SHELL` and `/etc/shells` for supported
families: Bash, POSIX sh, Zsh, Fish, Dash, Ksh and PowerShell. Windows additionally
checks common Git and PowerShell installation paths. Distinct installations
can appear separately. Probing inside WSL checks these Linux shell names on
the distribution's non-login `PATH`; it does not evaluate user login profiles.

This selector is available for the local process backend, including Windows
launching WSL. Remote execution peers keep their native shell and report that
selection is unavailable, rather than displaying the host's shells.

Regression coverage includes real POSIX process execution, command/panel/RPC
round trips and a real Python backend driven by the TUI client. Windows/WSL
coverage uses mocked platform and subprocess boundaries for UTF-16 discovery,
paths with spaces, Unicode, UNC workspaces, command arguments and failures;
it does not substitute for testing on a Windows host with installed WSL.
