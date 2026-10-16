# Data, backup and recovery

Each initialized project contains `.project-graph/project.json`, `events.jsonl` and a SQLite projection. Optional project registration, managed-runtime journals and code-index caches live under the application/project state directory. These files are excluded from Git.

Stop writers before copying the entire `.project-graph` directory. Keep original source files separately: an evidence reference does not bundle the file. Test a backup in a separate directory and inspect absolute project identity before switching it into use. Do not restore an old snapshot over events created after the backup.

The service stops on a malformed/incomplete event tail rather than silently discarding history. Do not edit the JSONL or SQLite by hand to bypass gates. Use CLI readback and explicit recovery work. A clean event replay and preserved later events are separate checks.

Stopping the foreground process does not delete state. Uninstalling this source checkout does not imply authorization to remove project directories elsewhere. This is a local single-user system; it is not an Internet-facing multi-user service.
