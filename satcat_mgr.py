"""
This script is a service that maintains an updated 3LE satellite catalog,
periodically querying GP data from space-track.org

Usage:
    python satcat_mgr.py <out_dir> <secrets_file>

Secrets file should be an INI format file containing a section like:

...
["space-track.org"]
username=ABC
password=XYZ
...
"""
import configparser
import sys
import logging
from logging.handlers import RotatingFileHandler
import argparse
from pathlib import Path
import sqlite3
import time

import requests

URL_BASE = "https://www.space-track.org"
LOGIN_URL = URL_BASE + "/ajaxauth/login"
QUERY_GP_URL = URL_BASE + "/basicspacedata/query/class/gp"

AUTH_TIMEOUT_SECS = 30
QUERY_TIMEOUT_SECS = 120

logger = logging.getLogger(__name__)

log_file = Path(__file__).with_suffix(".log")
db_file = Path(__file__).with_suffix(".db")


def kv_get(db_conn: sqlite3.Connection, key: str, default = None):
    """ Retrieve a value from the database's key-value store """
    row = db_conn.execute("SELECT value FROM kv_store WHERE key = ?;", (key,)).fetchone()
    return row[0] if row is not None else default

def kv_set(db_conn: sqlite3.Connection, key: str, value: str):
    """ Add/update a value in the database's key-value store """
    db_conn.execute("""
        INSERT INTO kv_store (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value
            ;
    """, (key, str(value)))
    db_conn.commit()

def spacetrack_login(session: requests.Session, username: str, password: str):
    """
    Authenticates a space-track.org session, using provided credentials
    """
    resp = session.post(
        LOGIN_URL,
        data={
            "identity": username,
            "password": password,
        },
        timeout=AUTH_TIMEOUT_SECS,
    )
    resp.raise_for_status()
    logger.debug("Authenticated space-track.org session")

def spacetrack_get_gp(session: requests.Session, days_to_query: float) -> list:
    url = QUERY_GP_URL + f"/decay_date/null-val/epoch/>now-{days_to_query:.2f}/orderby/NORAD_CAT_ID/format/json"
    logger.debug(f"Querying: {url}")
    response = session.get(url, timeout=QUERY_TIMEOUT_SECS)

    # basic handling for rate limiting
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            logger.debug(f"Rate-limit triggered, waiting for recommended {retry_after} seconds")
            time.sleep(int(retry_after))
        else:
            logger.warning("Rate-limit triggered, skipping this query")
            return []
        logger.debug("Retrying query")
        response = session.get(url, timeout=QUERY_TIMEOUT_SECS)

    response.raise_for_status()
    data = response.json()

    if not isinstance(data, list):
        raise RuntimeError(f"Expected list response, got {type(data)}")

    sats = []
    for row in data:
        try:
            sat = (
                row["NORAD_CAT_ID"].zfill(5),
                row["TLE_LINE0"],
                row["TLE_LINE1"],
                row["TLE_LINE2"],
            )
            sats.append(sat)
        except KeyError as e:
            logger.error(f"Malformed GP row: {e}")

    return sats

def update_sats(db_conn: sqlite3.Connection, sats: list):
    """
    Inserts sats into the database.

    Expects a list of tuples: (id, line0, line1, line2)
    """
    if len(sats) == 0:
        return

    cursor = db_conn.cursor()
    try:

        # insert sats
        cursor.executemany("""
            INSERT INTO latest_3les (object_id, line0, line1, line2)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(object_id) DO UPDATE SET
                    line0 = excluded.line0,
                    line1 = excluded.line1,
                    line2 = excluded.line2
                ;
        """, sats)

    finally:
        cursor.close()
    db_conn.commit()

    # update timestamp
    kv_set(db_conn, "last_updated_timestamp", str(time.time()))

def write_full_catalog(db_conn: sqlite3.Connection, file_path: Path):
    object_count = 0
    cursor = db_conn.cursor()
    try:
        cursor.execute("""
            SELECT line0, line1, line2 FROM latest_3les
                ORDER BY object_id;
        """)

        with open(file_path, "w") as f:
            while True:
                tle_lines = cursor.fetchone()
                if tle_lines is None:
                    break
                object_count += 1

                for line in tle_lines:
                    f.write(line + "\n")
    finally:
        cursor.close()

def write_individual_3le(db_conn: sqlite3.Connection, dir_path: Path, sat: tuple):
    """
    Writes individual 3LEs for a specific object

    Expects a list of tuples: (id, line0, line1, line2)
    """
    file_path = dir_path / (sat[0] + ".3le")
    with open(file_path, "w") as f:
        for line in sat[1:4]:
            f.write(line + "\n")


def main(out_dir: Path, secrets_file: Path):

    logger.info("Starting")
    logger.debug(f"Output directory: {out_dir}")
    logger.debug(f"Database file: {db_file}")

    logger.debug(f"Reading secrets file")
    secrets = configparser.ConfigParser()
    secrets.read(str(secrets_file))
    if "space-track.org" not in secrets:
        logger.critical("Provided secrets file missing required \"space-track.org\" section")
        sys.exit(1)

    spacetrack_username = secrets["space-track.org"].get("username", None)
    if spacetrack_username is None:
        logger.critical("Provided secrets file missing required \"space-track.org\" field: \"username\"")
        sys.exit(1)
    spacetrack_password = secrets["space-track.org"].get("password", None)
    if spacetrack_password is None:
        logger.critical("Provided secrets file missing required \"space-track.org\" field: \"password\"")
        sys.exit(1)

    if not out_dir.exists():
        logger.warning(f"Creating output directory: {out_dir}")
        out_dir.mkdir(mode=0o755, parents=True)
    elif not out_dir.is_dir():
        logger.critical("Provided output path is not a directory: \"{out_dir}\"")
        sys.exit(1)

    full_catalog_path = out_dir / "full_catalog.3le"
    individual_object_dir_path = out_dir / "by_object"
    individual_object_dir_path.mkdir(mode=0o755, exist_ok=True)

    logger.debug("Creating database connection")
    db_conn = sqlite3.connect(str(db_file))

    try:
        # ensure tables
        logger.debug("Ensuring db tables")
        db_conn.execute("""
            CREATE TABLE IF NOT EXISTS latest_3les (
                object_id TEXT PRIMARY KEY,
                line0 VARCHAR(25) NOT NULL,
                line1 VARCHAR(70) NOT NULL,
                line2 VARCHAR(70) NOT NULL
            );
        """)
        db_conn.execute("""
            CREATE TABLE IF NOT EXISTS kv_store (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)

        if not full_catalog_path.exists():
            write_full_catalog(db_conn, full_catalog_path)

        while True:
            # check last query time, sleep if necessary
            last_query_timestamp = float(kv_get(db_conn, "last_gp_query_time", "0.0"))
            secs_to_wait = (last_query_timestamp + 3600.0) - time.time()
            if secs_to_wait > 0.0:
                logger.info(f"Waiting {(secs_to_wait / 60.0):.2f} mins for next query")
                time.sleep(secs_to_wait)

            # query at most 10 days of data
            days_to_query = min((time.time() - last_query_timestamp) / 86400, 10.0)

            # make query for updated elsets
            logger.info(f"Pulling updated elsets (from last {days_to_query:.2f} days)")
            with requests.Session() as session:
                spacetrack_login(session, spacetrack_username, spacetrack_password)
                updated_sats = spacetrack_get_gp(session, days_to_query)
            # update last query time
            kv_set(db_conn, "last_gp_query_time", str(time.time()))

            if len(updated_sats) > 0:
                # update db/catalog
                logger.info(f"Received {len(updated_sats)} updated elsets, updating database and catalog files")
                update_sats(db_conn, updated_sats)
                
                write_full_catalog(db_conn, full_catalog_path)
                for sat in updated_sats:
                    write_individual_3le(db_conn, individual_object_dir_path, sat)
                
            else:
                logger.info("Received no updated elsets")
    finally:
        db_conn.close()
        

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog=__file__, 
        description="A script for creating and maintaining a 3LE satellite catalog",
    )
    parser.add_argument("out_dir", type=str, help="Path to directory to place 3LEs in")
    parser.add_argument("secrets_file", type=str, help="Path to INI file with credentials/secrets")

    args = parser.parse_args()

    logger.setLevel(logging.DEBUG)
    logger_sh = logging.StreamHandler()
    logger_sh.setFormatter(logging.Formatter("%(levelname)-8s %(message)s"))
    logger_sh.setLevel(logging.INFO)
    logger.addHandler(logger_sh)
    logger_fh = RotatingFileHandler(log_file, maxBytes=10000000, backupCount=9)
    logger_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s"))
    logger.addHandler(logger_fh)
    logger.debug("Logging initialized")

    try:
        main(Path(args.out_dir), Path(args.secrets_file))
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt")
    except Exception as e:
        logger.critical("Fatal exception", exc_info=True)
        sys.exit(1)
