"""Per-request SQLite connections and the FastAPI dependencies that hand them out.

* Reads use a read-only connection (`mode=ro`, `query_only`): a GET can never write, and WAL
  lets it run while `ingest` holds the write lock, so the site stays browsable mid-ingest.
* Writes use a short-lived normal connection with WAL, foreign keys and a busy timeout, wrapped
  in `jobhunt.db.transaction` (BEGIN IMMEDIATE ... COMMIT) by the caller.
* `check_same_thread=False`: FastAPI may run a sync dependency and its endpoint on different
  worker threads; a connection is still only ever used by one request at a time.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request

from jobhunt.config import Config
from jobhunt.db import connect

BUSY_TIMEOUT_MS = 30_000


def open_read_connection(database_path: Path) -> sqlite3.Connection:
    uri = f"{database_path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA query_only = ON")
    return connection


def get_config(request: Request) -> Config:
    if getattr(request.app.state, "demo_mode", False) and getattr(request.app.state, "demo_config", None):
        return request.app.state.demo_config
    config: Config = request.app.state.config
    return config


ConfigDep = Annotated[Config, Depends(get_config)]


def read_connection(config: ConfigDep) -> Iterator[sqlite3.Connection]:
    connection = open_read_connection(config.database_file)
    try:
        yield connection
    finally:
        connection.close()


def write_connection(config: ConfigDep) -> Iterator[sqlite3.Connection]:
    connection = connect(config.database_file, check_same_thread=False)
    try:
        yield connection
    finally:
        connection.close()


ReadDb = Annotated[sqlite3.Connection, Depends(read_connection)]
WriteDb = Annotated[sqlite3.Connection, Depends(write_connection)]
