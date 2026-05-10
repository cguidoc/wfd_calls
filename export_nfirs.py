"""
Export NFIRS PDR GeoPackage data for a department to the fire_vis JSON format.

Usage:
    python export_nfirs.py [--gpkg PATH]

Prompts interactively for state, town name, and year.
Pass --gpkg to override the default GeoPackage path; year defaults to the
4-digit number found in the filename (e.g. NFIRS_PDR_2024.gpkg → 2024).
"""

import argparse
import json
import math
import re
import sqlite3
import struct
import sys
from datetime import date


# ---------------------------------------------------------------------------
# NFIRS INC_TYPE → display category
# ---------------------------------------------------------------------------
def categorize(inc_type):
    try:
        t = int(inc_type)
    except (TypeError, ValueError):
        return 'other'
    if 100 <= t <= 199:
        return 'fire'
    if t in (322, 323, 324):          # vehicle accident with/without injuries
        return 'mva'
    if 300 <= t <= 399:
        return 'ems'
    if 400 <= t <= 499:
        return 'hazmat'
    if 500 <= t <= 599:
        return 'service'
    if 600 <= t <= 699:
        return 'goodintent'
    if 700 <= t <= 799:
        return 'falsealarm'
    if 800 <= t <= 899:
        return 'weather'
    return 'other'


# ---------------------------------------------------------------------------
# Timestamp helpers  (NFIRS format: MMDDYYYYHHMM, 12 chars)
# ---------------------------------------------------------------------------
def parse_ts(ts_str):
    """Return (date_obj, hh, mm) or None if unparseable."""
    if not ts_str or len(ts_str) < 12:
        return None
    try:
        mm = int(ts_str[0:2])
        dd = int(ts_str[2:4])
        yyyy = int(ts_str[4:8])
        hh = int(ts_str[8:10])
        mn = int(ts_str[10:12])
        return date(yyyy, mm, dd), hh, mn
    except (ValueError, OverflowError):
        return None


def parse_inc_date(inc_date_str):
    """MMDDYYYY → date object, or None."""
    if not inc_date_str or len(inc_date_str) < 8:
        return None
    try:
        mm = int(inc_date_str[0:2])
        dd = int(inc_date_str[2:4])
        yyyy = int(inc_date_str[4:8])
        return date(yyyy, mm, dd)
    except ValueError:
        return None


def day_of_year(d):
    return (d - date(d.year, 1, 1)).days + 1


# ---------------------------------------------------------------------------
# GPKG geometry blob → (lon, lat)
# ---------------------------------------------------------------------------
def gpkg_to_lonlat(blob):
    """Parse a GeoPackage geometry blob for a Point, return (lon, lat)."""
    if not blob or len(blob) < 8:
        return None, None
    flags = blob[3]
    env_code = (flags >> 1) & 0x07
    env_bytes = [0, 32, 48, 48, 64][env_code] if env_code <= 4 else 0
    wkb = blob[8 + env_bytes:]
    if len(wkb) < 21:
        return None, None
    fmt = '<' if wkb[0] == 1 else '>'
    x = struct.unpack(fmt + 'd', wkb[5:13])[0]
    y = struct.unpack(fmt + 'd', wkb[13:21])[0]
    if not math.isfinite(x) or not math.isfinite(y):
        return None, None
    return round(x, 6), round(y, 6)


# ---------------------------------------------------------------------------
# Address formatting
# ---------------------------------------------------------------------------
def fmt_address(match_addr):
    """'123 Main St, Watertown, Connecticut, 06795' → '123 Main St, Watertown'"""
    if not match_addr:
        return None
    parts = [p.strip() for p in match_addr.split(',')]
    # Keep street + city (first two meaningful parts)
    kept = [p for p in parts[:2] if p]
    return ', '.join(kept) if kept else match_addr


# ---------------------------------------------------------------------------
# Main export
# ---------------------------------------------------------------------------
def export(gpkg_path, state, fdid, year, out_path):
    conn = sqlite3.connect(gpkg_path)
    cur = conn.cursor()

    # Build INC_TYPE description lookup from codelookup table
    cur.execute("SELECT code_value, code_descr FROM codelookup WHERE fieldid='INC_TYPE'")
    inc_type_desc = {r[0]: r[1] for r in cur.fetchall()}

    # Pull all primary incidents (EXP_NO=0) for this dept × year,
    # joined to the geocoded incidentaddress table.
    # incidentaddress stores FDID without leading zeros but INCIDENT_KEY is canonical.
    key_prefix = f"{state}_{fdid}_%"
    date_prefix = f"%{year}"  # INC_DATE is MMDDYYYY, year in positions 4-7

    sql = """
        SELECT
            bi.INCIDENT_KEY,
            bi.INC_DATE,
            bi.INC_TYPE,
            bi.ALARM,
            bi.LU_CLEAR,
            bi.SUP_APP,
            bi.EMS_APP,
            bi.OTH_APP,
            ia.Shape,
            ia.Match_addr
        FROM basicincident bi
        LEFT JOIN incidentaddress ia ON ia.INCIDENT_KEY = bi.INCIDENT_KEY
        WHERE bi.STATE = ?
          AND bi.FDID = ?
          AND bi.EXP_NO = 0
          AND bi.INC_DATE LIKE ?
        ORDER BY bi.ALARM
    """
    cur.execute(sql, (state, fdid, date_prefix))
    rows = cur.fetchall()
    conn.close()

    if not rows:
        print(f"No incidents found for {state}/{fdid} in {year}.", file=sys.stderr)
        sys.exit(1)

    incidents = []
    skipped_no_geo = 0

    for row in rows:
        (ik, inc_date_s, inc_type, alarm_s, clear_s,
         sup_app, ems_app, oth_app, shape_blob, match_addr) = row

        lon, lat = gpkg_to_lonlat(shape_blob)
        if lat is None or lon is None:
            skipped_no_geo += 1
            continue

        d = parse_inc_date(inc_date_s)
        if d is None:
            continue

        alarm = parse_ts(alarm_s)
        clear = parse_ts(clear_s)

        tm_str = None
        ts_min = None
        if alarm:
            tm_str = f"{alarm[1]:02d}:{alarm[2]:02d}"
            ts_min = alarm[1] * 60 + alarm[2]

        duration = None
        if alarm and clear:
            a_min = alarm[1] * 60 + alarm[2]
            c_min = clear[1] * 60 + clear[2]
            # LU_CLEAR can be next day; basic check
            diff = c_min - a_min
            if diff < 0:
                diff += 1440  # next-day rollover
            if 0 <= diff <= 720:  # cap at 12 h — outliers likely bad data
                duration = diff

        inc_type_str = str(inc_type) if inc_type is not None else ''
        desc = inc_type_desc.get(inc_type_str) or inc_type_desc.get(str(int(inc_type_str)) if inc_type_str else '') or ''

        sup = sup_app or 0
        ems = ems_app or 0
        oth = oth_app or 0

        rec = {
            "la": lat,
            "lo": lon,
            "ik": ik,
            "dt": d.isoformat(),
            "dy": day_of_year(d),
            "mo": d.month,
            "ty": inc_type_str,
            "de": desc,
            "ca": categorize(inc_type),
            "ap": sup + ems + oth,
            "sa": sup,
            "ea": ems,
            "oa": oth,
            "ad": fmt_address(match_addr),
            "tm": tm_str,
            "ts": ts_min,
            "du": duration,
        }
        incidents.append(rec)

    print(f"Exported {len(incidents)} incidents ({skipped_no_geo} skipped — no geocode).")

    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(incidents, f, indent=2)
    print(f"Written to {out_path}")


# ---------------------------------------------------------------------------
def prompt(label, default):
    val = input(f"{label} [{default}]: ").strip()
    return val if val else default


def pick_department(conn, state, town):
    cur = conn.cursor()
    cur.execute(
        "SELECT FDID, FD_NAME FROM fdheader WHERE STATE=? AND FD_NAME LIKE ? ORDER BY FD_NAME",
        (state.upper(), f"%{town.upper()}%"),
    )
    rows = cur.fetchall()
    return rows


if __name__ == '__main__':
    p = argparse.ArgumentParser(description="Export NFIRS GPKG → fire_vis JSON")
    p.add_argument('--gpkg', default=r'data\nfirs_pdr_2024_gpkg\NFIRS_PDR_2024.gpkg')
    args = p.parse_args()

    # Default year from the year embedded in the GPKG filename
    m = re.search(r'(\d{4})', args.gpkg)
    default_year = m.group(1) if m else '2024'

    state = prompt("State", "CT")
    town  = prompt("Town / department name", "Watertown")
    year  = prompt("Year", default_year)

    conn = sqlite3.connect(args.gpkg)
    matches = pick_department(conn, state, town)
    conn.close()

    if not matches:
        print(f"No departments found in {state.upper()} matching '{town}'.")
        sys.exit(1)

    if len(matches) == 1:
        fdid, fd_name = matches[0]
        print(f"  Found: {fd_name}  (FDID {fdid})")
        confirm = input("Use this department? [Y/n]: ").strip().lower()
        if confirm == 'n':
            sys.exit(0)
    else:
        print(f"\n{len(matches)} departments found — pick one:")
        for i, (fdid, fd_name) in enumerate(matches, 1):
            print(f"  {i}) {fd_name}  (FDID {fdid})")
        while True:
            choice = input(f"Enter number [1-{len(matches)}]: ").strip()
            if choice.isdigit() and 1 <= int(choice) <= len(matches):
                fdid, fd_name = matches[int(choice) - 1]
                break
            print("  Invalid choice, try again.")

    slug = re.sub(r'\s+', '_', town.strip().lower())
    out_path = fr'data\{slug}_{state.lower()}_{year}.json'
    out_path = prompt("Output file", out_path)

    export(args.gpkg, state.upper(), fdid, year, out_path)
