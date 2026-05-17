from __future__ import annotations

import calendar
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

NS = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
MONTH_RE = re.compile(r"(?P<month>[A-Za-z]+)\s+(?P<year>\d{4})")


def main(argv: list[str]) -> int:
    if argv:
        workbook_paths = [Path(arg) for arg in argv]
    else:
        input_dir = Path("input")
        workbook_paths = sorted(input_dir.glob("*.xlsx"))
    workbook_paths = [path for path in workbook_paths if not path.name.startswith("~$")]

    if not workbook_paths:
        print("No .xlsx files found in input/.", file=sys.stderr)
        return 1

    records_by_year: dict[int, list[dict[str, object]]] = defaultdict(list)

    for workbook_path in workbook_paths:
        year, rows = convert_workbook(workbook_path)
        records_by_year[year].extend(rows)

    output_dir = Path("output")
    output_dir.mkdir(exist_ok=True)

    for year, records in sorted(records_by_year.items()):
        records.sort(key=lambda row: str(row["ts"]))
        out_path = output_dir / f"Streaming_History_Audio_{year}_0.json"
        out_path.write_text(json.dumps(records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {out_path} ({len(records)} rows)")

    return 0


def convert_workbook(workbook_path: Path) -> tuple[int, list[dict[str, object]]]:
    month, year = parse_month_year(workbook_path.name)
    rows = load_sheet_rows(workbook_path)
    output: list[dict[str, object]] = []

    for row_index, row in enumerate(rows, start=2):
        title = clean(row.get("Title"))
        album_artist = clean(row.get("Album Artist"))
        album = clean(row.get("Album"))

        if not title or not album_artist:
            continue

        played_at = synthetic_timestamp(workbook_path.name, row_index, year, month)
        output.append(
            {
                "ts": played_at.isoformat().replace("+00:00", "Z"),
                "platform": "Roon (synthetic from monthly export)",
                "ms_played": 0,
                "conn_country": "",
                "ip_addr_decrypted": "",
                "master_metadata_track_name": title,
                "master_metadata_album_artist_name": album_artist,
                "master_metadata_album_album_name": album,
                "spotify_track_uri": None,
                "episode_name": None,
                "episode_show_name": None,
                "spotify_episode_uri": None,
                "reason_start": "trackdone",
                "reason_end": "trackdone",
                "shuffle": False,
                "skipped": False,
                "offline": False,
                "offline_timestamp": 0,
                "incognito_mode": False,
            }
        )

    return year, output


def parse_month_year(filename: str) -> tuple[int, int]:
    match = MONTH_RE.search(filename)
    if not match:
        raise ValueError(f"Could not infer month/year from filename: {filename}")

    month_name = match.group("month").lower()
    year = int(match.group("year"))
    month = MONTHS.get(month_name)
    if month is None:
        raise ValueError(f"Unknown month in filename: {filename}")

    return month, year


def load_sheet_rows(workbook_path: Path) -> list[dict[str, str]]:
    with zipfile.ZipFile(workbook_path) as archive:
        shared_strings = load_shared_strings(archive)
        workbook_root = ET.fromstring(archive.read("xl/workbook.xml"))
        first_sheet = workbook_root.find("x:sheets/x:sheet", NS)
        if first_sheet is None:
            raise ValueError(f"No worksheets found in {workbook_path}")

        rel_id = first_sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]
        rels_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        rel_path = None
        for rel in rels_root:
            if rel.attrib.get("Id") == rel_id:
                rel_path = rel.attrib["Target"]
                break
        if rel_path is None:
            raise ValueError(f"Could not resolve worksheet path for {workbook_path}")

        rel_path = rel_path.lstrip("/")
        if rel_path.startswith("xl/"):
            sheet_path = rel_path
        else:
            sheet_path = f"xl/{rel_path}"

        sheet_xml = archive.read(sheet_path)
        sheet_root = ET.fromstring(sheet_xml)
        sheet_rows = sheet_root.findall("x:sheetData/x:row", NS)
        if not sheet_rows:
            return []

        headers = parse_row(sheet_rows[0], shared_strings)
        header_by_col = {column: value for column, value in headers.items() if value}

        output: list[dict[str, str]] = []
        for row in sheet_rows[1:]:
            values = parse_row(row, shared_strings)
            record = {
                header_by_col[column]: value
                for column, value in values.items()
                if column in header_by_col and value != ""
            }
            if record:
                output.append(record)
        return output


def load_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []

    strings: list[str] = []
    for item in root.findall("x:si", NS):
        text = "".join(node.text or "" for node in item.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t"))
        strings.append(text)
    return strings


def parse_row(row: ET.Element, shared_strings: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for cell in row.findall("x:c", NS):
        ref = cell.attrib.get("r", "")
        column = re.sub(r"\d", "", ref)
        cell_type = cell.attrib.get("t")
        value_node = cell.find("x:v", NS)
        value = value_node.text if value_node is not None and value_node.text is not None else ""
        if cell_type == "s" and value:
            value = shared_strings[int(value)]
        values[column] = value
    return values


def synthetic_timestamp(seed_text: str, row_index: int, year: int, month: int) -> datetime:
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    days_in_month = calendar.monthrange(year, month)[1]
    seconds_in_month = days_in_month * 24 * 60 * 60
    seed = hashlib.sha256(f"{seed_text}:{row_index}".encode("utf-8")).digest()
    seconds = int.from_bytes(seed[:8], "big") % seconds_in_month
    return start + timedelta(seconds=seconds)


def clean(value: str | None) -> str:
    return (value or "").strip()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
