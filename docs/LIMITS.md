# Limits

What WAKE does not do, and why. Each of these is a boundary of the design, stated so nobody relies on WAKE for
something it does not promise.

## Where it runs

- **Linux, kernel 5.3 or later, on aarch64 or x86_64.** WAKE uses ptrace with a seccomp filter and
  `PTRACE_GET_SYSCALL_INFO`. On macOS and Windows, run the agent inside Docker or a Linux virtual machine.
  The x86_64 filter is checked by the test suite's filter interpreter; the full trials in this repository were
  run on aarch64.

## What it governs

- **Destruction on the machine the agent runs on: files, folders and processes.** It is not a sandbox. The agent
  can still read anything you can and talk to the network. WAKE does not stop a secret from being read and sent.
  Use it alongside network controls, not instead of them.
- **Things done by another service are outside it.** A database server, the Docker daemon, systemd, a tmux server
  you already had running, a cloud API: WAKE sees the agent talk to them, not what they then do. If the agent asks
  the Docker daemon to remove your container, the daemon removes it. A SQLite database is a file and is covered;
  a PostgreSQL server is not. Adapters that route a service through WAKE (the Docker socket, the PostgreSQL wire
  protocol) are the planned answer.
- **Remote resources.** 26 of the 108 incidents in [INCIDENTS.md](INCIDENTS.md) destroyed something hosted
  elsewhere. WAKE does not reach them.

## Law 2, the stop

- WAKE stops every process the agent started, removes every file it made in a place that runs things later
  (shell start-up files, scheduled jobs, services, autostart entries, SSH keys, git hooks, Python start-up files,
  the dynamic linker), puts back your own start-up files it changed, and stops any other process running a file
  the agent made. One case remains: if the agent added a command (not a file) to your start-up file, and your own
  shell ran that command *before* you stopped the agent, the process it started keeps running. The line itself is
  removed, so it does not start again.
- An adopted process (`wake keep PID`) keeps running after the stop, unchecked, and WAKE stays running until it
  ends. A process carrying WAKE's filter cannot be released from it; without WAKE its checked calls would fail.

## Law 1, keeping

- **Identity.** A file is known by device, inode and birth time. The kernel records birth time to the nearest
  clock tick, so two files created at the same place within the same tick (a few milliseconds) can share an
  identity. In practice that means a file the agent made, then replaced by you within milliseconds.
- **Large files.** A file the agent is about to write over is copied first (a reflink where the filesystem
  supports one, which costs nothing). Without reflinks, writing over a very large file costs a full copy, once.
- **Restore puts back what you had before the agent first touched each thing.** If you also changed the same file
  during the session, your later version is moved into the vault, not lost. `wake restore PATH` restores one path.
- **WAKE's own records** are protected from the agent. Another program running as you can still change them.

## What the agent cannot do under WAKE

- **io_uring** is refused (programs fall back to ordinary calls): it would let file operations pass unchecked.
- **mount, kernel modules, bpf, reboot, kexec** are refused.
- **Setuid programs run without their privilege** (the filter sets no-new-privileges), so `sudo` does not work.
- **Debuggers** cannot attach to the agent's processes: WAKE is already attached.
- **Job control:** Ctrl-Z does not suspend a process under WAKE.

## Cost

Calls that could destroy something stop the agent while WAKE checks the graph: about 80 microseconds for each
file created and about 120 for each file deleted, measured in [TRIALS.md](TRIALS.md). Ordinary agent work (tests,
builds, git) costs a tenth to a fifth of a second per step; bulk work on tens of thousands of files costs seconds.
