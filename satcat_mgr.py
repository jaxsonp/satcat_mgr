"""
This script is a service that maintains an updated 3LE satellite catalog,
periodically querying GP data from space-track.org

Usage:
    python satcat_mgr.py <catalog_file> <secrets_file>

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
            logger.debug("Rate-limit triggered, skipping this query")
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

def write_catalog(db_conn: sqlite3.Connection, file_path: Path):
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
    logger.info(f"Wrote {object_count} objects to \"{file_path}\"")


def main(catalog_file: Path, secrets_file: Path):

    logger.info("Starting")
    logger.debug(f"Catalog file: {catalog_file}")
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

    if catalog_file.is_dir():
        logger.critical("Provided catalog file is a directory: \"{catalog_file}\"")
        sys.exit(1)

    db_needs_init = not db_file.exists()

    logger.debug("Creating database connection")
    db_conn = sqlite3.connect(str(db_file))

    try:
        if db_needs_init:
            logger.info("Initializing satellite database")

            # ensure tables
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

            # querying for initial sats to populate db
            with requests.Session() as session:
                spacetrack_login(session, spacetrack_username, spacetrack_password)

                logger.info("Querying 10 days of GP data (large query)")
                new_sats = spacetrack_get_gp(session, 10.0)
                # update last query time
                kv_set(db_conn, "last_gp_query_time", str(time.time()))

            logger.info(f"Received {len(new_sats)} elsets, inserting into database as a baseline")
            update_sats(db_conn, new_sats)
            logger.info("Database initialized")

        if not catalog_file.exists():
            logger.info(f"Catalog file \"{catalog_file}\" does not exist, writing now")
            write_catalog(db_conn, catalog_file)

        while True:
            # check last query time, sleep if necessary
            last_query_timestamp = float(kv_get(db_conn, "last_gp_query_time", "0.0"))
            secs_to_wait = (last_query_timestamp + 3600.0) - time.time()
            if secs_to_wait > 0.0:
                logger.info(f"Waiting {(secs_to_wait / 60.0):.2f} mins for next query")
                time.sleep(secs_to_wait)

            # make query for updated elsets
            with requests.Session() as session:
                spacetrack_login(session, spacetrack_username, spacetrack_password)

                days_to_query = (time.time() - last_query_timestamp) / 86400
                logger.info(f"Pulling updated elsets (from last {days_to_query:.2f} days)")
                updated_sats = spacetrack_get_gp(session, days_to_query)
                # update last query time
                kv_set(db_conn, "last_gp_query_time", str(time.time()))

            if len(updated_sats) > 0:
                # update db/catalog
                logger.info(f"Received {len(updated_sats)} updated elsets, updating database and catalog file")
                update_sats(db_conn, updated_sats)
                
                write_catalog(db_conn, catalog_file)
            else:
                logger.info("Received no updated elsets")
    finally:
        db_conn.close()
        

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog=__file__, 
        description="A script for creating and maintaining a 3LE satellite catalog",
    )
    parser.add_argument("catalog_file", type=str, help="Path of catalog file to create/update")
    parser.add_argument("secrets_file", type=str, help="Path to INI file with credentials/secrets")

    args = parser.parse_args()

    logger.setLevel(logging.DEBUG)
    logger_fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s")
    logger_sh = logging.StreamHandler()
    logger_sh.setFormatter(logger_fmt)
    logger.addHandler(logger_sh)
    logger_fh = RotatingFileHandler(log_file, maxBytes=10000000, backupCount=9)
    logger_fh.setFormatter(logger_fmt)
    logger.addHandler(logger_fh)
    logger.debug("Logging initialized")

    try:
        main(Path(args.catalog_file), Path(args.secrets_file))
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt")
    except Exception as e:
        logger.critical("Fatal exception", exc_info=True)
        sys.exit(1)
