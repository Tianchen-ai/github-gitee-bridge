"""Durable intent log, object mapping and a coalescing webhook inbox."""
import contextlib
import fcntl
import sqlite3
import time
from pathlib import Path


class State:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS objects (
                    repo TEXT NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL,
                    target TEXT, representation TEXT, pending INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(repo,kind,source), UNIQUE(repo,kind,target)
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    platform TEXT NOT NULL, id TEXT NOT NULL, repo TEXT NOT NULL,
                    received REAL NOT NULL, PRIMARY KEY(platform,id)
                );
                CREATE TABLE IF NOT EXISTS object_info (
                    repo TEXT NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL,
                    source_number TEXT, source_url TEXT, target_url TEXT,
                    canonical_state TEXT, head_sha TEXT,
                    PRIMARY KEY(repo,kind,source)
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    repo TEXT PRIMARY KEY, generation INTEGER NOT NULL DEFAULT 1,
                    done INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                    next_run REAL NOT NULL DEFAULT 0, last_success REAL, error TEXT
                );
            """)

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.directory / "bridge.sqlite3", timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextlib.contextmanager
    def worker_lock(self):
        with open(self.directory / "worker.lock", "a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Another worker uses this state directory") from None
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def get(self, repo, kind, source):
        with self.connect() as db:
            row = db.execute("SELECT * FROM objects WHERE repo=? AND kind=? AND source=?",
                             (repo, kind, str(source))).fetchone()
            return dict(row) if row else None

    def begin(self, repo, kind, source):
        with self.connect() as db:
            db.execute("INSERT INTO objects(repo,kind,source,pending) VALUES(?,?,?,1) "
                       "ON CONFLICT(repo,kind,source) DO UPDATE SET pending=1",
                       (repo, kind, str(source)))

    def save(self, repo, kind, source, target, representation):
        with self.connect() as db:
            db.execute("INSERT INTO objects VALUES(?,?,?,?,?,0) "
                       "ON CONFLICT(repo,kind,source) DO UPDATE SET target=excluded.target, "
                       "representation=excluded.representation,pending=0",
                       (repo, kind, str(source), str(target), representation))

    def clear_pending(self, repo, kind, source):
        with self.connect() as db:
            db.execute("DELETE FROM objects WHERE repo=? AND kind=? AND source=? AND target IS NULL",
                       (repo, kind, str(source)))

    def mapped_target(self, repo, kind, target):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM objects WHERE repo=? AND kind=? AND target=?",
                              (repo, kind, str(target))).fetchone() is not None

    def describe(self, repo, kind, source, target):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO object_info VALUES(?,?,?,?,?,?,?,?)", (
                repo, kind, str(source["id"]), str(source.get("number", source["id"])),
                source.get("html_url"), target.get("html_url"),
                "merged" if source.get("merged") else source.get("state"),
                (source.get("head") or {}).get("sha")))

    def mappings(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT o.*,i.source_number,i.source_url,i.target_url,i.canonical_state,i.head_sha "
                "FROM objects o LEFT JOIN object_info i USING(repo,kind,source) ORDER BY repo,kind,source")]

    def enqueue(self, repo, platform=None, delivery=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if delivery:
                result = db.execute("INSERT OR IGNORE INTO deliveries VALUES(?,?,?,?)",
                                    (platform, delivery, repo, time.time()))
                if not result.rowcount:
                    return False
            db.execute("INSERT INTO jobs(repo) VALUES(?) ON CONFLICT(repo) "
                       "DO UPDATE SET generation=generation+1", (repo,))
        return True

    def jobs(self, all_rows=False):
        with self.connect() as db:
            sql = "SELECT * FROM jobs" if all_rows else (
                "SELECT * FROM jobs WHERE generation>done AND next_run<=? ORDER BY next_run")
            return [dict(r) for r in db.execute(sql, () if all_rows else (time.time(),))]

    def finish(self, job, error=None):
        with self.connect() as db:
            if error:
                delay = min(3600, 10 * 2 ** min(job["attempts"], 9))
                db.execute("UPDATE jobs SET attempts=attempts+1,next_run=?,error=? WHERE repo=?",
                           (time.time() + delay, str(error)[:1000], job["repo"]))
            else:
                db.execute("UPDATE jobs SET done=?,attempts=0,next_run=0,last_success=?,error=NULL "
                           "WHERE repo=?", (job["generation"], time.time(), job["repo"]))
            db.execute("DELETE FROM deliveries WHERE received<?", (time.time() - 30 * 86400,))

    def status(self):
        with self.connect() as db:
            pending = [dict(r) for r in db.execute("SELECT * FROM objects WHERE pending=1")]
        return {"jobs": self.jobs(True), "pending": pending}
