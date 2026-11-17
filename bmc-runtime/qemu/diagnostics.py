"""Export and verify an offline BMC diagnostic archive."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import zipfile


def database_summary(db):
    if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ValueError("SQLite integrity check failed")
    return {
        "event_counts": [dict(source=source, generation=generation, count=count)
                         for source, generation, count in db.execute(
                             "SELECT source, generation, COUNT(*) FROM events "
                             "GROUP BY source, generation ORDER BY source, generation")],
        "alerts": [dict(source=source, generation=generation, kind=kind, first=first, last=last)
                   for source, generation, kind, first, last in db.execute(
                       "SELECT source,generation,kind,first_sequence,last_sequence FROM alerts ORDER BY id")],
    }


def inspect_metadata(files):
    appliance = json.loads(files["appliance.json"])
    guest = appliance["guest"]
    if guest not in ("linux", "arceos"):
        raise ValueError("unknown appliance type")
    timeline = [json.loads(line) for line in files["lifecycle.jsonl"].splitlines()]
    failures = [json.loads(files[name]) for name in sorted(files)
                if re.fullmatch(r"failure(?:-\d+)?\.json", name)]
    failures.sort(key=lambda failure: failure["generation"])
    return dict(guest=guest, lifecycle=timeline, failures=failures)


def export(source, destination):
    with tempfile.TemporaryDirectory(prefix="bmc-diagnostics-") as directory:
        snapshot = Path(directory) / "events.sqlite"
        with closing(sqlite3.connect((source / "events.sqlite").resolve().as_uri() + "?mode=ro", uri=True)) as db:
            with closing(sqlite3.connect(snapshot)) as copy:
                db.backup(copy)
                summary = database_summary(copy)
        files = {name: (source / name).read_bytes() for name in ("appliance.json", "lifecycle.jsonl")}
        for path in source.iterdir():
            if re.fullmatch(r"failure(?:-\d+)?\.json", path.name):
                files[path.name] = path.read_bytes()
        summary.update(inspect_metadata(files))
        files["events.sqlite"] = snapshot.read_bytes()
        files["summary.json"] = (json.dumps(summary, indent=2) + "\n").encode()
        manifest = dict(version=1, sha256={name: hashlib.sha256(data).hexdigest() for name, data in files.items()})
        with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in files.items():
                archive.writestr(name, data)
            archive.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")


def verify(path, *, include_events=False):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        manifest = json.loads(archive.read("manifest.json"))
        hashes = manifest["sha256"]
        if (manifest["version"] != 1 or len(names) != len(set(names))
                or set(names) != {*hashes, "manifest.json"}):
            raise ValueError("invalid archive manifest")
        required = {"appliance.json", "lifecycle.jsonl", "events.sqlite", "summary.json"}
        if not required <= hashes.keys() or any(
            name not in required and not re.fullmatch(r"failure(?:-\d+)?\.json", name) for name in hashes
        ):
            raise ValueError("invalid diagnostic files")
        files = {name: archive.read(name) for name in hashes}
        for name, digest in hashes.items():
            if hashlib.sha256(files[name]).hexdigest() != digest:
                raise ValueError(f"checksum mismatch for {name}")
    with tempfile.TemporaryDirectory(prefix="bmc-diagnostics-") as directory:
        snapshot = Path(directory) / "events.sqlite"
        snapshot.write_bytes(files["events.sqlite"])
        with closing(sqlite3.connect(snapshot)) as db:
            summary = database_summary(db)
            if include_events:
                events = [dict(generation=generation, sequence=sequence,
                               kind=event["kind"], sensor_id=event["sensor_id"])
                          for generation, sequence, payload in db.execute(
                              "SELECT generation, sequence, payload FROM events ORDER BY generation, sequence")
                          for event in (json.loads(payload),)]
    summary.update(inspect_metadata(files))
    if summary != json.loads(files["summary.json"]):
        raise ValueError("diagnostic summary does not match archive contents")
    if include_events:
        summary["events"] = events
    return summary


def compare(left_path, right_path):
    left, right = (verify(path, include_events=True) for path in (left_path, right_path))
    def reset_observations(summary):
        return [dict(generation=row["generation"], previous_sequence=row["first"],
                     observed_sequence=row["last"])
                for row in summary["alerts"] if row["kind"] == "stream_reset"]

    # A reset's observed sequence depends on when the collector first polls.
    # Missing-event boundaries, on the other hand, are part of the comparison.
    for summary in (left, right):
        summary["reset_observations"] = reset_observations(summary)
        summary["alerts"] = [dict(row, last=None) if row["kind"] == "stream_reset" else row
                             for row in summary["alerts"]]
    differences = {}
    for field, keys in (
        ("lifecycle", ("generation", "event", "reason", "previous_reason", "previous_generation")),
        ("failures", ("generation", "status", "reason")),
        ("events", ("generation", "sequence", "kind", "sensor_id")),
        ("alerts", ("generation", "kind", "first", "last")),
    ):
        a, b = ([{key: row.get(key) for key in keys} for row in summary[field]]
                for summary in (left, right))
        if a != b:
            index = next((index for index, pair in enumerate(zip(a, b)) if pair[0] != pair[1]),
                         min(len(a), len(b)))
            differences[field] = dict(index=index, left_count=len(a), right_count=len(b),
                                      left=a[index] if index < len(a) else None,
                                      right=b[index] if index < len(b) else None)
    return dict(matches=not differences, left=left["guest"], right=right["guest"],
                compared_events=[len(left["events"]), len(right["events"])],
                differences=differences,
                reset_observations=[left["reset_observations"], right["reset_observations"]])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    pack = commands.add_parser("export")
    pack.add_argument("source", type=Path)
    pack.add_argument("archive", type=Path)
    check = commands.add_parser("verify")
    check.add_argument("archive", type=Path)
    diff = commands.add_parser("compare")
    diff.add_argument("left", type=Path)
    diff.add_argument("right", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "export":
            export(args.source, args.archive)
        elif args.command == "verify":
            print(json.dumps(verify(args.archive), indent=2))
        else:
            report = compare(args.left, args.right)
            print(json.dumps(report, indent=2))
            if not report["matches"]:
                parser.exit(2)
    except (OSError, ValueError, KeyError, sqlite3.Error, zipfile.BadZipFile) as error:
        parser.exit(1, f"diagnostics: {error}\n")


if __name__ == "__main__":
    main()
