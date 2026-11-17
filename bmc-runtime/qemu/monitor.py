"""Archive BMC Runtime events and record discontinuities in a local SQLite database."""
import argparse
from contextlib import closing
from http.client import HTTPException
import json
from pathlib import Path
import sqlite3
import ssl
import sys
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

SCHEMA = """
CREATE TABLE IF NOT EXISTS cursor (
    source TEXT PRIMARY KEY, generation INTEGER NOT NULL,
    sequence INTEGER NOT NULL, anchor TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stream_identity (
    source TEXT PRIMARY KEY, stream_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    source TEXT NOT NULL, generation INTEGER NOT NULL,
    sequence INTEGER NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY (source, generation, sequence)
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY, source TEXT NOT NULL, generation INTEGER NOT NULL,
    kind TEXT NOT NULL, first_sequence INTEGER NOT NULL, last_sequence INTEGER NOT NULL
);
"""


def encode(event):
    return json.dumps(event, sort_keys=True, separators=(",", ":"))


def ingest(db, source, journal):
    events = journal["events"]
    overwritten = journal["overwritten"]
    stream_id = journal.get("stream_id")
    if stream_id is not None and (not isinstance(stream_id, str) or len(stream_id) != 32
                                  or any(c not in "0123456789abcdefABCDEF" for c in stream_id)):
        raise ValueError("invalid stream identity")
    if type(overwritten) is not int or overwritten < 0 or (overwritten and not events):
        raise ValueError("invalid event retention counter")
    for sequence, event in enumerate(events, overwritten + 1):
        if event["sequence"] != sequence:
            raise ValueError("event snapshot contains a discontinuity")
    latest = overwritten + len(events)
    alerts = []
    with db:
        # Serialize readers updating the same source across processes.
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute(
            "SELECT generation, sequence, anchor FROM cursor WHERE source = ?", (source,)
        ).fetchone()
        generation, last, anchor = previous or (1, 0, "")
        identity = db.execute("SELECT stream_id FROM stream_identity WHERE source = ?", (source,)).fetchone()
        if identity and stream_id is None:
            raise ValueError("stream identity missing")
        changed = identity is not None and identity[0] != stream_id
        if changed or latest < last or (overwritten < last <= latest and encode(events[last - overwritten - 1]) != anchor):
            generation += 1
            alerts.append(("stream_reset", last, latest))
            last = 0
        if overwritten > last:
            alerts.append(("events_missing", last + 1, overwritten))
        for kind, first, end in alerts:
            db.execute(
                "INSERT INTO alerts(source,generation,kind,first_sequence,last_sequence) VALUES (?,?,?,?,?)",
                (source, generation, kind, first, end),
            )
        new = [event for event in events if event["sequence"] > last]
        db.executemany("INSERT INTO events VALUES (?,?,?,?)",
                       [(source, generation, event["sequence"], encode(event)) for event in new])
        db.execute("INSERT OR REPLACE INTO cursor VALUES (?,?,?,?)",
                   (source, generation, latest, encode(events[-1]) if events else ""))
        if stream_id is not None:
            db.execute("INSERT OR REPLACE INTO stream_identity VALUES (?,?)", (source, stream_id))
    return {"archived": len(new), "generation": generation, "sequence": latest,
            "alerts": [{"kind": kind, "first": first, "last": end} for kind, first, end in alerts]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="BMC base URL")
    parser.add_argument("database", help="SQLite archive path")
    parser.add_argument("--source", help="stable source identifier, defaults to the base URL")
    parser.add_argument("--interval", type=float, default=0.25)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--token-file", type=Path, help="file containing the reader bearer token")
    parser.add_argument("--ca-file", type=Path, help="trusted CA bundle for HTTPS")
    args = parser.parse_args()
    if not 0 < args.interval < float("inf"):
        parser.error("--interval must be positive and finite")
    url = args.url.rstrip("/")
    source = args.source or url
    context = ssl.create_default_context(cafile=str(args.ca_file) if args.ca_file else None)
    with closing(sqlite3.connect(args.database)) as db:
        db.executescript(SCHEMA)
        while True:
            try:
                headers = {}
                if args.token_file:
                    headers["Authorization"] = "Bearer " + args.token_file.read_text().strip()
                with urlopen(Request(url + "/v1/events", headers=headers), timeout=2, context=context) as response:
                    journal = json.load(response)
                report = ingest(db, source, journal)
                if args.once or report["archived"] or report["alerts"]:
                    print(json.dumps(report), flush=True)
            except (OSError, URLError, HTTPException, ValueError, TypeError, KeyError) as error:
                print(f"{source}: {error}", file=sys.stderr, flush=True)
                if args.once:
                    return 1
            if args.once:
                return 0
            time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
