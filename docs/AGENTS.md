# Instructions for an agent running under WAKE

Give these to the agent (for example in `CLAUDE.md`, `AGENTS.md` or its system prompt). WAKE works without them;
with them the agent stops guessing which processes and files are its own.

---

You are running under WAKE. Every process you start and every file you make is recorded as yours. Anything that
was here before you started belongs to the person, and WAKE keeps a copy before you change or delete it.

- **To see what is yours:** `wake mine` lists the processes you started that are still running and where the
  files you made are.
- **To stop your own processes:** `wake kill-mine PATTERN` stops only your processes whose command matches.
  Do not use `pkill -f` or `killall`: they match the person's processes too, and WAKE will refuse those.
- **If a kill fails with "Operation not permitted",** that process is the person's. Leave it alone and tell them.
- **To check who made something:** `wake whose PATH` or `wake whose PID`.
- **When you change or delete the person's files,** say so plainly in your summary. Their originals are kept and
  `wake restore` puts them back, but the person should know what you touched.

For direct questions, the whole record is a Relate graph you can query in Cypher, read-only:
`relate serve --database "$WAKE_HOME/sessions/$WAKE_SESSION/body" --read-only=live --mcp stdio` serves it to an
MCP host.
