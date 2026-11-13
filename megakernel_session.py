#!/usr/bin/env python3
"""Export the MegaKernel Codex chat, or relocate an extracted export."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3

ROOT = "01a0180e-ab9d-72b1-8a0e-24db65f200fa"
MANIFEST = "megakernel-session.json"


def read_db(path):
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)


def quoted(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def export_db(source, destination, filters, ids):
    """Recreate the source schema, copying only explicitly selected rows."""
    placeholders = ",".join("?" for _ in ids)
    with read_db(source) as original, sqlite3.connect(destination) as exported:
        original.execute("BEGIN")
        schema = original.execute(
            "SELECT type, name, sql FROM sqlite_schema "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY rowid"
        ).fetchall()
        for kind, name, sql in schema:
            if kind != "table":
                continue
            exported.execute(sql)
            if name not in filters:
                continue
            terms = filters[name]
            condition = " AND ".join(
                f"{quoted(column)} IN ({placeholders})" for column in terms
            )
            query = f"SELECT * FROM {quoted(name)}"
            if condition:
                query += " WHERE " + condition
            rows = original.execute(query, ids * len(terms))
            values = ",".join("?" for _ in rows.description)
            exported.executemany(f"INSERT INTO {quoted(name)} VALUES ({values})", rows)
        if source.name == "state_5.sqlite":
            exported.execute(
                "UPDATE threads SET project_id = NULL, thread_section_id = NULL, "
                "section_position = NULL, section_entered_at_ms = NULL"
            )
        for kind, _, sql in schema:
            if kind != "table":
                exported.execute(sql)
        errors = exported.execute("PRAGMA foreign_key_check").fetchall()
        if errors:
            raise RuntimeError(f"Missing referenced rows in {source.name}: {errors[:3]}")


def pack(source, destination):
    state_path = source / "state_5.sqlite"
    with read_db(state_path) as state:
        thread_ids = tuple(row[0] for row in state.execute(
            """
            WITH RECURSIVE related(id) AS (
                VALUES (?)
                UNION
                SELECT child_thread_id FROM thread_spawn_edges JOIN related
                    ON parent_thread_id = related.id
                UNION
                SELECT threads.id FROM threads JOIN related
                    ON attached_thread_parent_id = related.id
            )
            SELECT id FROM related ORDER BY id
            """, (ROOT,)
        ))
        rollout_paths = dict(state.execute(
            "SELECT id, rollout_path FROM threads WHERE id IN "
            f"({','.join('?' for _ in thread_ids)})", thread_ids
        ))
    if set(rollout_paths) != set(thread_ids):
        raise RuntimeError("Some related chats have no state record")

    files = set()
    history_ids = set(thread_ids)
    for directory in ("sessions", "archived_sessions"):
        for path in (source / directory).rglob("rollout-*.jsonl*"):
            stem, extension = path.name.split(".jsonl", 1)
            if extension not in ("", ".zst"):
                continue
            file_id, _, segment_id = stem[28:].partition("_")
            if file_id in thread_ids:
                files.add(path.relative_to(source))
                if segment_id:
                    history_ids.add(segment_id)

    relative_rollouts = {
        thread_id: Path(path).relative_to(source)
        for thread_id, path in rollout_paths.items()
    }
    for thread_id, relative in relative_rollouts.items():
        compressed = relative.with_suffix(relative.suffix + ".zst")
        if relative not in files and compressed in files:
            relative_rollouts[thread_id] = compressed
    missing = set(relative_rollouts.values()) - files
    if missing:
        raise RuntimeError(f"Missing rollouts: {sorted(str(path) for path in missing)}")

    destination.mkdir(mode=0o700)
    for relative in sorted(files):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, target)

    export_db(state_path, destination / state_path.name, {
        "_sqlx_migrations": (),
        "backfill_state": (),
        "threads": ("id",),
        "thread_spawn_edges": ("parent_thread_id", "child_thread_id"),
        "thread_dynamic_tools": ("thread_id",),
        "thread_attachments": ("thread_id",),
        "thread_read_receipts": ("thread_id",),
    }, thread_ids)
    history_name = "thread_history_1.sqlite"
    export_db(source / history_name, destination / history_name, {
        "_sqlx_migrations": (),
        "thread_turns": ("thread_id",),
        "thread_items": ("thread_id",),
        "thread_realtime_items": ("thread_id",),
        "thread_history_projection_state": ("thread_id",),
    }, tuple(sorted(history_ids)))

    goals_name = "goals_1.sqlite"
    if (source / goals_name).is_file():
        export_db(source / goals_name, destination / goals_name, {
            "_sqlx_migrations": (),
            "thread_goals": ("thread_id",),
            "thread_goal_continuation_deferrals": ("thread_id",),
        }, thread_ids)

    index_path = source / "session_index.jsonl"
    if index_path.is_file():
        with index_path.open() as index, (destination / index_path.name).open("w") as copied:
            for line in index:
                if json.loads(line).get("id") in thread_ids:
                    copied.write(line)
    (destination / MANIFEST).write_text(json.dumps({
        "root": ROOT,
        "rollouts": {key: path.as_posix() for key, path in relative_rollouts.items()},
        "files": [path.as_posix() for path in sorted(files)],
    }, indent=2) + "\n")
    relocate(destination)


def relocate(destination):
    manifest = json.loads((destination / MANIFEST).read_text())
    if manifest["root"] != ROOT:
        raise RuntimeError("This export has a different root chat")
    for filename in manifest["files"]:
        if not (destination / filename).is_file():
            raise RuntimeError(f"Missing file: {filename}")
    with sqlite3.connect(destination / "state_5.sqlite") as state:
        state.executemany(
            "UPDATE threads SET rollout_path = ? WHERE id = ?",
            [(str(destination / path), key) for key, path in manifest["rollouts"].items()],
        )
    print(f"{len(manifest['rollouts'])} chats, {len(manifest['files'])} rollout files")
    print(f"Codex storage directory: {destination}")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    pack_args = commands.add_parser("pack", help="create a new isolated Codex storage directory")
    pack_args.add_argument("destination", type=Path)
    pack_args.add_argument("--source", type=Path, default=Path.home() / ".codex")
    commands.add_parser("relocate", help="update paths after moving the exported directory").add_argument(
        "destination", type=Path
    )
    args = parser.parse_args()
    destination = args.destination.expanduser().resolve()
    if args.action == "pack":
        pack(args.source.expanduser().resolve(), destination)
    else:
        relocate(destination)


if __name__ == "__main__":
    main()
