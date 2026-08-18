from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


MAX_HISTORY_PDF_BYTES = 50 * 1024 * 1024
PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ReportHistoryEntry:
    report_id: str
    created_at: datetime
    cutoff: date
    instrument_count: int
    total_amount: float
    source_id: str
    rule_version: str
    pdf_path: Path

    @property
    def download_name(self) -> str:
        return f"cartera_historica_{self.cutoff:%Y-%m-%d}_{self.created_at:%Y%m%d_%H%M}.pdf"


def history_directory(configured: str | Path | None = None) -> Path:
    """Devuelve el directorio persistente compartido por los usuarios autorizados."""
    configured_value = str(configured or os.getenv("CARTERA_REPORT_HISTORY_DIR", "")).strip()
    directory = Path(configured_value) if configured_value else PROJECT_ROOT / "data" / "report_history"
    if not directory.is_absolute():
        directory = PROJECT_ROOT / directory
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _write_once(path: Path, data: bytes) -> bool:
    try:
        with path.open("xb") as stream:
            stream.write(data)
        return True
    except FileExistsError:
        return False


def _entry_from_metadata(metadata: dict, pdf_path: Path) -> ReportHistoryEntry:
    created_at = datetime.fromisoformat(str(metadata["created_at"]))
    return ReportHistoryEntry(
        report_id=str(metadata["report_id"]),
        created_at=created_at,
        cutoff=date.fromisoformat(str(metadata["cutoff"])),
        instrument_count=max(0, int(metadata["instrument_count"])),
        total_amount=float(metadata["total_amount"]),
        source_id=str(metadata["source_id"]),
        rule_version=str(metadata.get("rule_version", "")),
        pdf_path=pdf_path,
    )


def save_report_snapshot(
    report_bytes: bytes,
    *,
    source_digest: str,
    cutoff: date,
    instrument_count: int,
    total_amount: float,
    rule_version: str,
    directory: str | Path | None = None,
    created_at: datetime | None = None,
) -> tuple[ReportHistoryEntry, bool]:
    """Guarda una sola versión por archivo, corte y regla sin conservar el Excel fuente."""
    if not report_bytes.startswith(b"%PDF") or len(report_bytes) > MAX_HISTORY_PDF_BYTES:
        raise ValueError("El reporte no es un PDF válido o supera el límite permitido.")
    normalized_digest = source_digest.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized_digest):
        raise ValueError("El identificador del archivo fuente no es válido.")

    target_dir = history_directory(directory)
    version_seed = f"{normalized_digest}:{cutoff.isoformat()}:{rule_version}".encode("utf-8")
    report_id = hashlib.sha256(version_seed).hexdigest()[:20]
    stem = f"cartera_{cutoff:%Y%m%d}_{normalized_digest[:12]}_{report_id[-8:]}"
    pdf_path = target_dir / f"{stem}.pdf"
    metadata_path = target_dir / f"{stem}.json"
    timestamp = created_at or datetime.now().astimezone()
    metadata = {
        "report_id": report_id,
        "created_at": timestamp.isoformat(timespec="seconds"),
        "cutoff": cutoff.isoformat(),
        "instrument_count": int(instrument_count),
        "total_amount": float(total_amount),
        "source_id": normalized_digest[:12],
        "rule_version": rule_version,
    }

    created = _write_once(pdf_path, report_bytes)
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    else:
        _write_once(metadata_path, json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8"))

    return _entry_from_metadata(metadata, pdf_path), created


def list_report_history(directory: str | Path | None = None, limit: int = 200) -> list[ReportHistoryEntry]:
    target_dir = history_directory(directory)
    entries: list[ReportHistoryEntry] = []
    for metadata_path in target_dir.glob("cartera_*.json"):
        pdf_path = metadata_path.with_suffix(".pdf")
        if not pdf_path.is_file() or pdf_path.resolve().parent != target_dir:
            continue
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            entries.append(_entry_from_metadata(metadata, pdf_path))
        except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
            continue
    entries.sort(key=lambda entry: entry.created_at, reverse=True)
    return entries[: max(1, min(int(limit), 500))]


def read_report_snapshot(entry: ReportHistoryEntry) -> bytes:
    data = entry.pdf_path.read_bytes()
    if not data.startswith(b"%PDF") or len(data) > MAX_HISTORY_PDF_BYTES:
        raise ValueError("El reporte histórico no es un PDF válido.")
    return data
