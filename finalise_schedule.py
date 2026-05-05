import base64
import json
import re
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).parent
WEB_DIR = PROJECT_ROOT / "web"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"


def _pdf_escape(value: str) -> str:
    return str(value or "").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _week_bounds(schedule_data: dict) -> tuple[str, str, str]:
    rows = [
        row
        for loc_rows in (schedule_data.get("locations") or {}).values()
        for row in (loc_rows or [])
        if row.get("date")
    ]
    first_date = min((row["date"] for row in rows), default=date.today().isoformat())
    start = date.fromisoformat(first_date)
    week_start = start - timedelta(days=start.weekday())
    week_end = week_start + timedelta(days=6)
    label = f"{week_start.strftime('%d %b')} - {week_end.strftime('%d %b %Y')}"
    return week_start.isoformat(), week_end.isoformat(), label


def _schedule_lines(schedule_data: dict, week_label: str) -> list[str]:
    lines = [
        "Physique 57 India - Finalised Weekly Schedule",
        f"Week: {week_label}",
        "",
    ]
    by_location = schedule_data.get("locations") or {}
    for location in sorted(by_location):
        slots = sorted(
            by_location.get(location) or [],
            key=lambda s: (s.get("date", ""), s.get("time", ""), s.get("class_name", "")),
        )
        lines.append(location)
        lines.append("-" * min(92, len(location) + 8))
        if not slots:
            lines.append("No classes scheduled.")
            lines.append("")
            continue
        for s in slots:
            fill = s.get("predicted_fill_rate")
            fill_text = f"{float(fill) * 100:.0f}%" if isinstance(fill, (int, float)) else "-"
            avg = s.get("historical_avg_checkin")
            avg_text = f"{float(avg):.1f}" if isinstance(avg, (int, float)) else "-"
            lines.append(
                " | ".join(
                    [
                        f"{s.get('date', '')} {s.get('day_of_week', '')}".strip(),
                        s.get("time", ""),
                        s.get("class_name", ""),
                        f"Trainer: {s.get('trainer_1', '')}",
                        f"Room: {s.get('room', '')}",
                        f"Fill: {fill_text}",
                        f"Avg: {avg_text}",
                    ]
                )
            )
        lines.append("")
    return lines


def _make_pdf(lines: list[str]) -> bytes:
    page_line_limit = 46
    pages = [lines[i : i + page_line_limit] for i in range(0, len(lines), page_line_limit)] or [[]]
    objects: list[bytes] = []

    def add(obj: str | bytes) -> int:
        objects.append(obj.encode("latin-1") if isinstance(obj, str) else obj)
        return len(objects)

    add("<< /Type /Catalog /Pages 2 0 R >>")
    add("")  # pages object placeholder
    font_id = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids: list[int] = []

    for page_lines in pages:
        content_lines = ["BT", "/F1 9 Tf", "40 760 Td", "12 TL"]
        for idx, line in enumerate(page_lines):
            text = line[:118]
            if idx == 0:
                content_lines.append(f"({_pdf_escape(text)}) Tj")
            else:
                content_lines.append(f"T* ({_pdf_escape(text)}) Tj")
        content_lines.append("ET")
        stream = "\n".join(content_lines).encode("latin-1", errors="replace")
        content_id = add(
            b"<< /Length " + str(len(stream)).encode("latin-1") + b" >>\nstream\n" + stream + b"\nendstream"
        )
        page_id = add(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>"
        )
        page_ids.append(page_id)

    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects[1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode("latin-1")

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for idx, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out.extend(f"{idx} 0 obj\n".encode("latin-1"))
        out.extend(obj)
        out.extend(b"\nendobj\n")
    xref_start = len(out)
    out.extend(f"xref\n0 {len(objects) + 1}\n".encode("latin-1"))
    out.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        out.extend(f"{offset:010d} 00000 n \n".encode("latin-1"))
    out.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_start}\n%%EOF\n".encode(
            "latin-1"
        )
    )
    return bytes(out)


def _summary(schedule_data: dict) -> dict:
    by_location = {}
    trainer_counts = defaultdict(int)
    total = 0
    for location, rows in (schedule_data.get("locations") or {}).items():
        rows = rows or []
        by_location[location] = len(rows)
        total += len(rows)
        for row in rows:
            if row.get("trainer_1"):
                trainer_counts[row["trainer_1"]] += 1
    return {
        "total_classes": total,
        "classes_by_location": by_location,
        "classes_by_trainer": dict(sorted(trainer_counts.items())),
    }


def finalise_schedule_document(
    supabase_request: Callable[..., Any],
    schedule_path: Path | None = None,
    outputs_dir: Path | None = None,
) -> dict:
    schedule_path = schedule_path or (WEB_DIR / "schedule_data.json")
    outputs_dir = outputs_dir or OUTPUTS_DIR
    if not schedule_path.exists():
        raise FileNotFoundError("web/schedule_data.json was not found")

    schedule_data = json.loads(schedule_path.read_text(encoding="utf-8"))
    week_start, week_end, week_label = _week_bounds(schedule_data)
    lines = _schedule_lines(schedule_data, week_label)
    pdf_bytes = _make_pdf(lines)
    outputs_dir.mkdir(exist_ok=True)
    filename = f"finalised_schedule_{week_start}.pdf"
    local_path = outputs_dir / filename
    local_path.write_bytes(pdf_bytes)

    payload = {
        "week_start": week_start,
        "week_end": week_end,
        "status": "finalised",
        "file_name": filename,
        "mime_type": "application/pdf",
        "file_base64": base64.b64encode(pdf_bytes).decode("ascii"),
        "schedule_data": schedule_data,
        "summary": _summary(schedule_data),
    }
    row = supabase_request(
        "POST",
        "/finalised_schedules?on_conflict=week_start",
        [payload],
        "resolution=merge-duplicates,return=representation",
    )
    public_id = row[0].get("id") if isinstance(row, list) and row else None
    return {
        "week_start": week_start,
        "week_end": week_end,
        "file_name": filename,
        "local_path": str(local_path),
        "supabase_id": public_id,
        "bytes": len(pdf_bytes),
    }
