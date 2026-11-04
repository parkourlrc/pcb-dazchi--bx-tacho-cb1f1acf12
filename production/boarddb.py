# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Darren Chi
"""The production database: one SQLite table, one row per board, keyed by the
chip's eFuse unique ID. Flashing fills the flash columns, the VBAT
calibration the cal_ ones.

    python boarddb.py import records.csv vbat_cal.csv   # the CSV files of earlier versions
"""

import argparse
import datetime
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

VERIFY_TOL_MV = 10   # the firmware's VBAT_CAL_VERIFY_TOL_MV when the CSV checks were made (15 from 2.4.1)

SCHEMA = '''
CREATE TABLE IF NOT EXISTS boards (
    unique_id            TEXT PRIMARY KEY,   -- eFuse unique ID, 32 hex digits
    serial_mac           TEXT NOT NULL,      -- factory MAC: the serial number
    bt_mac               TEXT,
    chip                 TEXT,
    flashed_at           TEXT,               -- the last flash (ISO time)
    firmware             TEXT,
    firmware_sha256      TEXT,
    flash_port           TEXT,
    flash_count          INTEGER NOT NULL DEFAULT 0,
    fw_update_at         TEXT,               -- the last firmware update (Update tab: settings and
    fw_update_count      INTEGER NOT NULL DEFAULT 0,   -- calibration kept)
    cal_at               TEXT,               -- the VBAT calibration (cleared when flashed again:
    cal_gain             REAL,               -- erasing the chip erases it)
    cal_offset_mv        INTEGER,
    cal_points           INTEGER,
    cal_max_fit_error_mv INTEGER,
    cal_verify           TEXT,               -- actual:error; ...
    cal_result           TEXT,               -- PASS | FAIL; CAL: calibrated elsewhere, read from
                                             --   the board's `status` (gain, offset, points, fit error)
    cal_ppk2_offset_mv   INTEGER,
    cal_attempts         INTEGER,
    cal_detail           TEXT,               -- JSON: {points: [...], verify: [...]}
    cal_count            INTEGER NOT NULL DEFAULT 0,
    batt_cal             INTEGER,            -- read from the board ('status', Database tab):
    batt_curve           TEXT,               --   1 if its battery percentage curve was measured
    black_load_empty_mv  INTEGER,            --   ('cal batt'); the load sensor calibration and
    black_load_loaded_mv INTEGER,            --   RPM threshold of each launcher ('cal load',
    black_rpm_thr_mv     INTEGER,            --   'rpm thr'); load NULL: not calibrated
    white_load_empty_mv  INTEGER,
    white_load_loaded_mv INTEGER,
    white_rpm_thr_mv     INTEGER,
    board_read_at        TEXT,               -- when these were read (cleared by a flash)
    customer_name        TEXT,               -- entered on the Database tab; kept when flashed again
    customer_contact     TEXT,               -- phone, social media account or email
    warranty_since       TEXT,               -- YYYY-MM-DD
    notes                TEXT,
    updated_at           TEXT NOT NULL
)
'''

COLUMNS = ['unique_id', 'serial_mac', 'bt_mac', 'chip', 'flashed_at', 'firmware', 'firmware_sha256', 'flash_port',
           'flash_count', 'fw_update_at', 'fw_update_count', 'cal_at', 'cal_gain', 'cal_offset_mv', 'cal_points', 'cal_max_fit_error_mv', 'cal_verify',
           'cal_result', 'cal_ppk2_offset_mv', 'cal_attempts', 'cal_detail', 'cal_count', 'batt_cal', 'batt_curve',
           'black_load_empty_mv', 'black_load_loaded_mv', 'black_rpm_thr_mv', 'white_load_empty_mv',
           'white_load_loaded_mv', 'white_rpm_thr_mv', 'board_read_at', 'customer_name',
           'customer_contact', 'warranty_since', 'notes', 'updated_at']
CAL_COLUMNS = [c for c in COLUMNS if c.startswith('cal_')]
# Read from the board's `status` (erased by a flash, so cleared then)
BOARD_COLUMNS = ['batt_cal', 'batt_curve', 'black_load_empty_mv', 'black_load_loaded_mv', 'black_rpm_thr_mv',
                 'white_load_empty_mv', 'white_load_loaded_mv', 'white_rpm_thr_mv', 'board_read_at']
CUSTOMER_COLUMNS = ['customer_name', 'customer_contact', 'warranty_since', 'notes']
# Columns added after the first version, with their types: added to older databases
ADDED_COLUMNS = {'customer_name': 'TEXT', 'customer_contact': 'TEXT', 'warranty_since': 'TEXT', 'notes': 'TEXT',
                 'fw_update_at': 'TEXT', 'fw_update_count': 'INTEGER NOT NULL DEFAULT 0',
                 **{c: 'TEXT' if c in ('batt_curve', 'board_read_at') else 'INTEGER' for c in BOARD_COLUMNS}}


class NoUniqueId(RuntimeError):
    pass


def now():
    return datetime.datetime.now().isoformat(timespec='seconds')


def connect(path):
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init(path):
    with closing(connect(path)) as conn:
        conn.execute('PRAGMA journal_mode=WAL')   # the page reads while a worker writes
        conn.execute(SCHEMA)
        # A database made by an earlier version: add the columns it lacks
        have = {r['name'] for r in conn.execute('PRAGMA table_info(boards)')}
        for c, kind in ADDED_COLUMNS.items():
            if c not in have:
                conn.execute(f'ALTER TABLE boards ADD COLUMN {c} {kind}')
        conn.commit()


def save_customer(path, uid, fields):
    """The customer columns of one board, from the Database tab. Blank fields
    are stored as NULL. Raises ValueError for a bad date or an unknown board."""
    values = {c: (str(fields.get(c) or '').strip() or None) for c in CUSTOMER_COLUMNS}
    if values['warranty_since']:
        try:
            datetime.date.fromisoformat(values['warranty_since'])
        except ValueError:
            raise ValueError('warranty since: use a date like 2026-11-04') from None
    with closing(connect(path)) as conn, conn:
        cur = conn.execute(
            'UPDATE boards SET ' + ', '.join(f'{c} = ?' for c in CUSTOMER_COLUMNS) + ', updated_at = ? '
            'WHERE unique_id = ?', (*values.values(), now(), uid))
        if cur.rowcount == 0:
            raise ValueError(f'no board with unique ID {uid}')


def delete(path, uid):
    """Remove one board's row (Database tab). Returns it, as it was."""
    with closing(connect(path)) as conn, conn:
        row = conn.execute('SELECT * FROM boards WHERE unique_id = ?', (uid,)).fetchone()
        if row is None:
            raise ValueError(f'no board with unique ID {uid}')
        conn.execute('DELETE FROM boards WHERE unique_id = ?', (uid,))
        return dict(row)


def _uid(ids):
    if not ids.get('unique_id'):
        raise NoUniqueId(f'{ids.get("serial_mac")}: no eFuse unique ID on this chip, so it can\'t be recorded')
    return ids['unique_id']


def save_flash(path, ids, firmware, sha, port, at=None):
    """A board was flashed (the chip erased, so its calibration is gone).
    Returns its flash count."""
    uid = _uid(ids)
    at = at or now()
    with closing(connect(path)) as conn, conn:
        conn.execute(
            'INSERT INTO boards (unique_id, serial_mac, bt_mac, chip, flashed_at, firmware, firmware_sha256, '
            'flash_port, flash_count, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?) '
            'ON CONFLICT (unique_id) DO UPDATE SET serial_mac = excluded.serial_mac, bt_mac = excluded.bt_mac, '
            'chip = excluded.chip, flashed_at = excluded.flashed_at, firmware = excluded.firmware, '
            'firmware_sha256 = excluded.firmware_sha256, flash_port = excluded.flash_port, '
            'flash_count = flash_count + 1, updated_at = excluded.updated_at, '
            + ', '.join(f'{c} = NULL' for c in CAL_COLUMNS + BOARD_COLUMNS if c != 'cal_count'),
            (uid, ids['serial_mac'], ids.get('bt_mac'), ids.get('chip'), at, firmware, sha, port, at))
        return conn.execute('SELECT flash_count FROM boards WHERE unique_id = ?', (uid,)).fetchone()[0]


def save_update(path, ids, firmware, sha, port, at=None):
    """A board's firmware was updated (settings and calibration kept).
    Returns its update count."""
    uid = _uid(ids)
    at = at or now()
    with closing(connect(path)) as conn, conn:
        conn.execute(
            'INSERT INTO boards (unique_id, serial_mac, bt_mac, chip, fw_update_at, firmware, firmware_sha256, '
            'flash_port, fw_update_count, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?) '
            'ON CONFLICT (unique_id) DO UPDATE SET serial_mac = excluded.serial_mac, bt_mac = excluded.bt_mac, '
            'chip = excluded.chip, fw_update_at = excluded.fw_update_at, firmware = excluded.firmware, '
            'firmware_sha256 = excluded.firmware_sha256, flash_port = excluded.flash_port, '
            'fw_update_count = fw_update_count + 1, updated_at = excluded.updated_at',
            (uid, ids['serial_mac'], ids.get('bt_mac'), ids.get('chip'), at, firmware, sha, port, at))
        return conn.execute('SELECT fw_update_count FROM boards WHERE unique_id = ?', (uid,)).fetchone()[0]


def get(path, uid):
    with closing(connect(path)) as conn:
        r = conn.execute('SELECT * FROM boards WHERE unique_id = ?', (uid,)).fetchone()
        return dict(r) if r else None


def save_ids(path, ids):
    """Record a board's IDs without flashing it (--identify-only, or added on
    the Database tab), and the firmware version it runs if ids has 'firmware'"""
    uid = _uid(ids)
    with closing(connect(path)) as conn, conn:
        conn.execute(
            'INSERT INTO boards (unique_id, serial_mac, bt_mac, chip, firmware, updated_at) '
            'VALUES (?, ?, ?, ?, ?, ?) '
            'ON CONFLICT (unique_id) DO UPDATE SET serial_mac = excluded.serial_mac, '
            'bt_mac = COALESCE(excluded.bt_mac, bt_mac), chip = COALESCE(excluded.chip, chip), '
            'firmware = COALESCE(excluded.firmware, firmware), updated_at = excluded.updated_at',
            (uid, ids['serial_mac'], ids.get('bt_mac'), ids.get('chip'), ids.get('firmware'), now()))


STATUS_CAL_COLUMNS = ['cal_gain', 'cal_offset_mv', 'cal_points', 'cal_max_fit_error_mv', 'cal_result']


def save_board_state(path, uid, st):
    """What a board's `status` says about its calibrations (BOARD_COLUMNS but
    board_read_at; a key left out of st, e.g. by an older firmware that
    doesn't print it, keeps its value).

    st['vbat'], if there, is its VBAT calibration: a dict with gain,
    offset_mv, points, max_fit_error_mv, or None for none. It is recorded
    with cal_result CAL only for a board without a calibration by this tool
    (PASS/FAIL, which says more), and a CAL goes again when the board has
    none any more.

    The last change (updated_at) only moves if a value did. Returns False if
    the board isn't in the database."""
    with closing(connect(path)) as conn, conn:
        row = conn.execute('SELECT * FROM boards WHERE unique_id = ?', (uid,)).fetchone()
        if row is None:
            return False
        values = {c: st[c] for c in BOARD_COLUMNS if c in st and c != 'board_read_at'}
        if 'vbat' in st and row['cal_result'] in (None, 'CAL'):
            v = st['vbat']
            values.update(zip(STATUS_CAL_COLUMNS, (float(v['gain']), v['offset_mv'], v['points'],
                                                   v['max_fit_error_mv'], 'CAL') if v else (None,) * 5))
        at = now()
        changed = any(row[c] != values[c] for c in values)
        conn.execute(
            'UPDATE boards SET ' + ''.join(f'{c} = ?, ' for c in values) + 'board_read_at = ?'
            + (', updated_at = ?' if changed else '') + ' WHERE unique_id = ?',
            (*values.values(), at, *((at,) if changed else ()), uid))
        return True


def save_cal(path, ids, cal, detail, at=None):
    """A VBAT calibration: `cal` has gain, offset_mv, points, max_fit_error_mv,
    verify, result, ppk2_offset_mv, attempts; `detail` the points and checks"""
    uid = _uid(ids)
    at = at or now()
    values = (float(cal['gain']), cal['offset_mv'], cal['points'], cal['max_fit_error_mv'], cal['verify'],
              cal['result'], cal['ppk2_offset_mv'], cal['attempts'], json.dumps(detail))
    with closing(connect(path)) as conn, conn:
        conn.execute(
            'INSERT INTO boards (unique_id, serial_mac, bt_mac, chip, cal_at, cal_gain, cal_offset_mv, cal_points, '
            'cal_max_fit_error_mv, cal_verify, cal_result, cal_ppk2_offset_mv, cal_attempts, cal_detail, '
            'cal_count, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?) '
            'ON CONFLICT (unique_id) DO UPDATE SET serial_mac = excluded.serial_mac, '
            'bt_mac = COALESCE(excluded.bt_mac, bt_mac), chip = COALESCE(excluded.chip, chip), '
            'cal_at = excluded.cal_at, cal_gain = excluded.cal_gain, cal_offset_mv = excluded.cal_offset_mv, '
            'cal_points = excluded.cal_points, cal_max_fit_error_mv = excluded.cal_max_fit_error_mv, '
            'cal_verify = excluded.cal_verify, cal_result = excluded.cal_result, '
            'cal_ppk2_offset_mv = excluded.cal_ppk2_offset_mv, cal_attempts = excluded.cal_attempts, '
            'cal_detail = excluded.cal_detail, cal_count = cal_count + 1, updated_at = excluded.updated_at',
            (uid, ids['serial_mac'], ids.get('bt_mac'), ids.get('chip'), at, *values, at))


def rows(path):
    """Every board, the latest change first"""
    with closing(connect(path)) as conn:
        return [dict(r) for r in conn.execute('SELECT * FROM boards ORDER BY updated_at DESC, unique_id')]


def summary(path, n=10):
    """Counts and the latest flashes and calibrations, for the Flash and VBAT tabs"""
    with closing(connect(path)) as conn:
        def q(sql):
            return [dict(r) for r in conn.execute(sql)]
        return {
            'flashed': conn.execute('SELECT COUNT(*) FROM boards WHERE flashed_at IS NOT NULL').fetchone()[0],
            'updated': conn.execute('SELECT COUNT(*) FROM boards WHERE fw_update_at IS NOT NULL').fetchone()[0],
            'calibrated': conn.execute('SELECT COUNT(*) FROM boards WHERE cal_at IS NOT NULL').fetchone()[0],
            'boards': conn.execute('SELECT COUNT(*) FROM boards').fetchone()[0],
            'recent_flash': q('SELECT unique_id, serial_mac, bt_mac, flashed_at, firmware, flash_count FROM boards '
                              f'WHERE flashed_at IS NOT NULL ORDER BY flashed_at DESC LIMIT {int(n)}'),
            'recent_update': q('SELECT unique_id, serial_mac, bt_mac, fw_update_at, firmware, fw_update_count, '
                               'cal_result FROM boards WHERE fw_update_at IS NOT NULL '
                               f'ORDER BY fw_update_at DESC LIMIT {int(n)}'),
            'recent_cal': q('SELECT unique_id, serial_mac, cal_at, cal_gain, cal_offset_mv, cal_max_fit_error_mv, '
                            'cal_result, cal_attempts FROM boards WHERE cal_at IS NOT NULL '
                            f'ORDER BY cal_at DESC LIMIT {int(n)}'),
        }


# ---------------------------------------------------------------- the CSV files of earlier versions

def import_csv(path, records_csv=None, cal_csv=None):
    """Replay records.csv and vbat_cal.csv in time order, as if each row had
    just been saved: a flash after a calibration clears it, as it erased it.
    Returns (rows imported, rows skipped)."""
    import csv
    events = []
    for kind, file in (('flash', records_csv), ('cal', cal_csv)):
        if file:
            with open(file, newline='', encoding='utf-8') as f:
                events += [(r['timestamp'], kind, r) for r in csv.DictReader(f)]
    events.sort(key=lambda e: (e[0], e[1] == 'cal'))
    init(path)
    done = skipped = 0
    for at, kind, r in events:
        ids = {'unique_id': r.get('unique_id'), 'serial_mac': r['serial_mac'], 'bt_mac': r.get('bt_mac') or None,
               'chip': r.get('chip') or None}
        try:
            if kind == 'flash' and r.get('firmware', '').startswith('not flashed'):
                save_ids(path, ids)
            elif kind == 'flash':
                save_flash(path, ids, r['firmware'], r['firmware_sha256'], r['port'], at)
                if 'flashed again' in r.get('note', ''):
                    # The CSV kept only the latest flash: count the one it replaced
                    with closing(connect(path)) as conn, conn:
                        conn.execute('UPDATE boards SET flash_count = flash_count + 1 WHERE unique_id = ?',
                                     (ids['unique_id'],))
            else:
                verify = []
                for item in filter(None, (v.strip() for v in r['verify'].split(';'))):
                    actual, err = item.split(':')
                    verify.append({'actual': int(actual), 'measured': int(actual) + int(err), 'err': int(err),
                                   'pass': abs(int(err)) <= VERIFY_TOL_MV})
                cal = {'gain': r['gain'], 'offset_mv': int(r['offset_mv']), 'points': int(r['points']),
                       'max_fit_error_mv': int(r['max_fit_error_mv']), 'verify': r['verify'],
                       'result': r['result'], 'ppk2_offset_mv': int(r['ppk2_offset_mv'] or 0),
                       'attempts': int(r['attempts']) if r.get('attempts') else None}
                save_cal(path, ids, cal, {'points': [], 'verify': verify}, at)
            done += 1
        except NoUniqueId as e:
            print(f'skipped ({kind}, {at}): {e}')
            skipped += 1
    return done, skipped


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    imp = sub.add_parser('import', help='import the CSV files of earlier versions')
    imp.add_argument('records_csv', help='records.csv (flashing)')
    imp.add_argument('cal_csv', nargs='?', help='vbat_cal.csv (VBAT calibration)')
    imp.add_argument('--db', default='production.db', help='database (default production.db)')
    args = ap.parse_args()
    db = Path(args.db)
    if db.exists() and rows(db):
        sys.exit(f'{db} already has boards in it: import into a new database')
    done, skipped = import_csv(db, args.records_csv, args.cal_csv)
    print(f'{done} rows imported, {skipped} skipped: {len(rows(db))} boards in {db.resolve()}')


if __name__ == '__main__':
    main()
