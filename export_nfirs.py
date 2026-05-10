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
import os
import re
import sqlite3
import statistics
import struct
import sys
from collections import Counter
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

    stats = analyze(incidents)
    stats_path = re.sub(r'\.json$', '_stats.json', out_path)
    with open(stats_path, 'w', encoding='utf-8') as f:
        json.dump(stats, f, indent=2)
    print(f"Stats written to {stats_path}")


# ---------------------------------------------------------------------------
# Post-export analytics
# ---------------------------------------------------------------------------
DOW_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']


def analyze(incidents):
    if not incidents:
        return {}

    total = len(incidents)

    # --- category counts ---
    cat_counts = Counter(r['ca'] for r in incidents)

    # --- month / hour / day-of-week distributions ---
    month_counts = Counter(r['mo'] for r in incidents)
    hour_counts  = Counter(int(r['ts'] // 60) for r in incidents if r['ts'] is not None)
    dow_counts   = Counter(
        DOW_NAMES[date.fromisoformat(r['dt']).weekday()]
        for r in incidents
    )

    # --- date range ---
    dates = sorted(r['dt'] for r in incidents)

    # --- busiest single day ---
    day_counts = Counter(r['dt'] for r in incidents)
    busiest_date, busiest_count = day_counts.most_common(1)[0]

    # --- top addresses (skip None) ---
    addr_counts = Counter(r['ad'] for r in incidents if r['ad'])
    top_addresses = [
        {'address': addr, 'count': cnt}
        for addr, cnt in addr_counts.most_common(10)
    ]

    # --- top incident types ---
    type_counter = Counter((r['ty'], r['de']) for r in incidents if r['ty'])
    top_types = [
        {'type': ty, 'description': de, 'count': cnt}
        for (ty, de), cnt in type_counter.most_common(15)
    ]

    # --- apparatus stats ---
    ap_values = [r['ap'] for r in incidents]
    max_ap    = max(ap_values)
    max_ap_incident = next(
        {'ik': r['ik'], 'dt': r['dt'], 'ad': r['ad'], 'ca': r['ca'],
         'de': r['de'], 'ap': r['ap'], 'sa': r['sa'], 'ea': r['ea'], 'oa': r['oa']}
        for r in incidents if r['ap'] == max_ap
    )
    avg_ap = round(sum(ap_values) / total, 2)
    multi_ap_calls = sum(1 for v in ap_values if v > 1)

    # --- duration stats ---
    durations = [r['du'] for r in incidents if r['du'] is not None]
    dur_stats = None
    if durations:
        dur_stats = {
            'calls_with_data': len(durations),
            'avg_minutes':    round(statistics.mean(durations), 1),
            'median_minutes': round(statistics.median(durations), 1),
            'max_minutes':    max(durations),
            'min_minutes':    min(durations),
        }

    return {
        'generated':    date.today().isoformat(),
        'total_calls':  total,
        'date_range':   {'first': dates[0], 'last': dates[-1]},
        'calls_by_category': dict(sorted(cat_counts.items())),
        'calls_by_month':    {str(m): month_counts.get(m, 0) for m in range(1, 13)},
        'calls_by_hour':     {str(h): hour_counts.get(h, 0)  for h in range(24)},
        'calls_by_dow':      {d: dow_counts.get(d, 0) for d in DOW_NAMES},
        'busiest_day':       {'date': busiest_date, 'count': busiest_count},
        'top_addresses':     top_addresses,
        'top_incident_types': top_types,
        'apparatus': {
            'avg_per_call':           avg_ap,
            'max_single_call':        max_ap,
            'max_single_call_record': max_ap_incident,
            'multi_apparatus_calls':  multi_ap_calls,
        },
        'duration': dur_stats,
    }


# ---------------------------------------------------------------------------
def prompt(label, default):
    val = input(f"{label} [{default}]: ").strip()
    return val if val else default


def pick_department(conn, state, town):
    """Return list of (fdid, name) if fdheader exists, else None."""
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='fdheader'")
    if not cur.fetchone():
        return None
    cur.execute(
        "SELECT FDID, FD_NAME FROM fdheader WHERE STATE=? AND FD_NAME LIKE ? ORDER BY FD_NAME",
        (state.upper(), f"%{town.upper()}%"),
    )
    return cur.fetchall()


def list_fdids(conn, state):
    """Return sorted list of FDIDs present in basicincident for this state."""
    cur = conn.cursor()
    cur.execute(
        "SELECT DISTINCT FDID FROM basicincident WHERE STATE=? ORDER BY FDID",
        (state.upper(),),
    )
    return [r[0] for r in cur.fetchall()]


def find_gpkg_files(search_dir='data'):
    """Recursively find non-empty .gpkg files under search_dir."""
    results = []
    for root, _dirs, files in os.walk(search_dir):
        for name in files:
            if name.lower().endswith('.gpkg'):
                full = os.path.join(root, name)
                if os.path.getsize(full) > 0:
                    results.append(full)
    return sorted(results)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description="Export NFIRS GPKG → fire_vis JSON")
    p.add_argument('--gpkg', help="Path to GeoPackage file (skips file picker)")
    p.add_argument('--fdid', help="Skip department lookup and use this FDID directly")
    args = p.parse_args()

    # Resolve the GPKG to use
    if args.gpkg:
        gpkg_path = args.gpkg
        if not os.path.isfile(gpkg_path):
            print(f"Error: file not found: {gpkg_path}", file=sys.stderr)
            sys.exit(1)
        if os.path.getsize(gpkg_path) == 0:
            print(f"Error: file is empty (0 bytes): {gpkg_path}", file=sys.stderr)
            sys.exit(1)
    else:
        gpkg_files = find_gpkg_files()
        if not gpkg_files:
            print("No .gpkg files found under data\\.  Pass --gpkg to specify a path.", file=sys.stderr)
            sys.exit(1)
        if len(gpkg_files) == 1:
            gpkg_path = gpkg_files[0]
            print(f"Using: {gpkg_path}")
        else:
            print("GeoPackage files found:")
            for i, f in enumerate(gpkg_files, 1):
                print(f"  {i}) {f}")
            while True:
                choice = input(f"Select file [1-{len(gpkg_files)}]: ").strip()
                if choice.isdigit() and 1 <= int(choice) <= len(gpkg_files):
                    gpkg_path = gpkg_files[int(choice) - 1]
                    break
                print("  Invalid choice, try again.")

    # Default year from the year embedded in the GPKG filename
    m = re.search(r'(\d{4})', os.path.basename(gpkg_path))
    default_year = m.group(1) if m else '2024'

    state = prompt("State", "CT")
    town  = prompt("Town / department name", "Watertown")
    year  = prompt("Year", default_year)

    if args.fdid:
        fdid = args.fdid
    else:
        conn = sqlite3.connect(gpkg_path)
        matches = pick_department(conn, state, town)

        if matches is None:
            # fdheader table not in this GPKG — show available FDIDs and ask
            fdids = list_fdids(conn, state)
            conn.close()
            if not fdids:
                print(f"No incidents found for state {state.upper()} in this file.")
                sys.exit(1)
            print(f"\nfdheader table not available in this GeoPackage.")
            print(f"FDIDs with data for {state.upper()} ({len(fdids)} total):")
            for fid in fdids:
                print(f"  {fid}")
            fdid = input("Enter FDID: ").strip()
            if not fdid:
                sys.exit(1)
        else:
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
                for i, (fid, fd_name) in enumerate(matches, 1):
                    print(f"  {i}) {fd_name}  (FDID {fid})")
                while True:
                    choice = input(f"Enter number [1-{len(matches)}]: ").strip()
                    if choice.isdigit() and 1 <= int(choice) <= len(matches):
                        fdid, fd_name = matches[int(choice) - 1]
                        break
                    print("  Invalid choice, try again.")

    slug = re.sub(r'\s+', '_', town.strip().lower())
    out_path = fr'data\{slug}_{state.lower()}_{year}.json'
    out_path = prompt("Output file", out_path)

    export(gpkg_path, state.upper(), fdid, year, out_path)
