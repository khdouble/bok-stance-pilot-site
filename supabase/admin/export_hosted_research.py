#!/usr/bin/env python3
"""Export and validate the PII-free hosted pilot snapshot from PostgreSQL."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


REPOSITORY = Path(__file__).resolve().parents[2]
COLLECTOR_PATH = REPOSITORY / "tools" / "collect_hosted_pilot.py"
COLLECTOR_SPEC = importlib.util.spec_from_file_location("collect_hosted_pilot_for_export", COLLECTOR_PATH)
if COLLECTOR_SPEC is None or COLLECTOR_SPEC.loader is None:
    raise RuntimeError(f"cannot load hosted collector: {COLLECTOR_PATH}")
COLLECTOR = importlib.util.module_from_spec(COLLECTOR_SPEC)
COLLECTOR_SPEC.loader.exec_module(COLLECTOR)

RESPONSE_QUERY_PATH = Path(__file__).with_name("research_responses_export.sql")
FEEDBACK_QUERY_PATH = Path(__file__).with_name("research_feedback_export.sql")


def required_database_url(environment: Mapping[str, str]) -> str:
    value = environment.get("SUPABASE_DB_URL", "").strip()
    if not value:
        raise ValueError("SUPABASE_DB_URL must be set in the current process")
    if not value.startswith(("postgresql://", "postgres://")) or any(character.isspace() for character in value):
        raise ValueError("SUPABASE_DB_URL must be a PostgreSQL URI without whitespace")
    return value


def query_text(path: Path) -> str:
    query = path.read_text(encoding="utf-8")
    if query.count("%s") != 2:
        raise ValueError(f"{path.name} must contain exactly two bound parameters")
    lowered = query.lower()
    for forbidden in (
        "private.", "participant_id", "invite_id", "identity_ciphertext", "identity_hmac",
        "consent_version", "consent_accepted_at", "token_hmac", "client_user_agent",
    ):
        if forbidden in lowered:
            raise ValueError(f"{path.name} contains forbidden identity field: {forbidden}")
    direct_identifier = re.search(r"\b(?:name|phone)\b", lowered)
    if direct_identifier:
        raise ValueError(
            f"{path.name} contains forbidden identity field: {direct_identifier.group(0)}"
        )
    return query


def csv_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("database returned a timezone-naive timestamp")
        utc = value.astimezone(timezone.utc)
        return utc.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return str(value)


def cursor_columns(cursor: Any) -> tuple[str, ...]:
    description = cursor.description
    if description is None:
        raise ValueError("database query returned no column description")
    return tuple(str(column.name if hasattr(column, "name") else column[0]) for column in description)


def execute_exact_query(
    connection: Any,
    query: str,
    parameters: tuple[str, str],
    expected_columns: tuple[str, ...],
) -> list[dict[str, str]]:
    cursor = connection.execute(query, parameters)
    columns = cursor_columns(cursor)
    if columns != expected_columns:
        raise ValueError(f"database export columns differ from the hosted contract: {columns!r}")
    rows: list[dict[str, str]] = []
    for result in cursor.fetchall():
        if len(result) != len(columns):
            raise ValueError("database export row width differs from its header")
        rows.append({column: csv_value(value) for column, value in zip(columns, result, strict=True)})
    return rows


def fetch_snapshot(
    connection: Any,
    instrument_sha256: str,
    instrument_version: str,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    response_query = query_text(RESPONSE_QUERY_PATH)
    feedback_query = query_text(FEEDBACK_QUERY_PATH)
    with connection.transaction():
        connection.execute("set transaction isolation level repeatable read read only")
        connection.execute("set local statement_timeout = '30s'")
        responses = execute_exact_query(
            connection,
            response_query,
            (instrument_sha256, instrument_version),
            COLLECTOR.RESPONSE_COLUMNS,
        )
        feedback = execute_exact_query(
            connection,
            feedback_query,
            (instrument_sha256, instrument_version),
            COLLECTOR.FEEDBACK_COLUMNS,
        )
    return responses, feedback


def write_raw_csv(path: Path, columns: tuple[str, ...], rows: Iterable[dict[str, str]]) -> None:
    with path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def validate_and_write(
    responses: list[dict[str, str]],
    feedback: list[dict[str, str]],
    instrument_path: Path,
    instrument_sha256: str,
    assignments: dict[str, list[dict[str, Any]]],
    expected_submissions: int,
    output: Path,
    repository: Path = REPOSITORY,
) -> Path:
    validated = COLLECTOR.validate_collection(
        responses,
        feedback,
        instrument_sha256,
        assignments,
        expected_submissions,
    )
    with tempfile.TemporaryDirectory(prefix="hosted-pilot-export-") as directory:
        scratch = Path(directory)
        response_input = scratch / "responses.csv"
        feedback_input = scratch / "feedback.csv"
        write_raw_csv(response_input, COLLECTOR.RESPONSE_COLUMNS, responses)
        write_raw_csv(feedback_input, COLLECTOR.FEEDBACK_COLUMNS, feedback)
        return COLLECTOR.write_collection(
            output,
            repository,
            validated,
            response_input,
            feedback_input,
            instrument_path,
            instrument_sha256,
        )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--expected-submissions", type=int, choices=(3, 4, 5), required=True)
    result.add_argument("--output", type=Path, required=True)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    instrument_path = REPOSITORY / "docs" / "instrument.json"
    try:
        output = COLLECTOR.ensure_output(args.output, REPOSITORY)
        instrument_sha256, assignments = COLLECTOR.load_instrument(instrument_path)
        database_url = required_database_url(os.environ)
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("psycopg is required: python -m pip install 'psycopg[binary]'") from exc
        with psycopg.connect(
            database_url,
            connect_timeout=10,
            application_name="bok-hosted-pilot-export",
            sslmode="require",
        ) as connection:
            responses, feedback = fetch_snapshot(
                connection, instrument_sha256, COLLECTOR.HOSTED_VERSION,
            )
        manifest = validate_and_write(
            responses,
            feedback,
            instrument_path,
            instrument_sha256,
            assignments,
            args.expected_submissions,
            output,
        )
    except (ValueError, FileExistsError, OSError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception:
        # Driver failures can include credentials in their text; never echo them.
        print("ERROR: database export failed; no credentials or row data were printed.", file=sys.stderr)
        return 1
    print(
        f"PASS hosted PII-free export submissions={args.expected_submissions} "
        f"responses={args.expected_submissions * COLLECTOR.RESPONSE_COUNT} manifest={manifest}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
