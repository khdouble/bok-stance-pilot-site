#!/usr/bin/env python3
"""Finalize a disposable remote E2E only after verified database cleanup."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from tools import run_remote_e2e as e2e


FINAL_STATUS = "E2E_VERIFIED_CLEAN"
DEFAULT_FINAL_RECEIPT = "remote-e2e-clean-receipt.json"
MAX_PENDING_AGE = timedelta(hours=24)
PENDING_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "tested_config_timestamp",
        "site_url",
        "api_url",
        "browser_family",
        "browser_version",
        "hosted_version",
        "instrument_sha256",
        "deployment_manifest_sha256",
        "source_attestation_sha256",
        "asset_attestations",
        "config_override_sha256",
        "published_hold_preserved",
        "config_intercept_count",
        "browser_asset_count",
        "assignment_count",
        "initial_submit_http_status",
        "initial_submit_ok",
        "same_retry_http_status",
        "same_retry_idempotent",
        "changed_retry_http_status",
        "changed_retry_rejected",
        "fragment_removed",
        "private_inputs_cleared",
        "draft_cleared",
        "javascript_exception_count",
        "database_record_count",
        "cleanup_required",
        "fielding_authorized",
        "auto_cleanup_result",
    }
)


class FinalizeError(RuntimeError):
    """A safe finalization refusal that contains no private target value."""


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.exit(2, "ERROR: invalid command-line arguments; use --help\n")


def parser() -> argparse.ArgumentParser:
    result = SafeArgumentParser(description=__doc__)
    result.add_argument("--private-root", type=Path)
    result.add_argument("--pending-receipt", type=Path)
    result.add_argument("--invite-file", type=Path)
    result.add_argument("--output", type=Path)
    return result


def validate_pending_receipt(
    receipt: Mapping[str, Any],
    *,
    now: datetime,
) -> datetime:
    if set(receipt) != PENDING_KEYS:
        raise FinalizeError("pending receipt schema is invalid")
    if (
        receipt.get("schema_version") != "1.0"
        or receipt.get("status") != e2e.PENDING_STATUS
        or receipt.get("site_url") != e2e.SITE_URL
        or receipt.get("api_url") != e2e.API_URL
        or receipt.get("cleanup_required") is not True
        or receipt.get("fielding_authorized") is not False
        or receipt.get("published_hold_preserved") is not True
    ):
        raise FinalizeError("pending receipt identity or state is invalid")

    timestamp = receipt.get("tested_config_timestamp")
    if not isinstance(timestamp, str) or not e2e.UTC_RE.fullmatch(timestamp):
        raise FinalizeError("pending receipt tested timestamp is invalid")
    tested_at = e2e._parse_utc(timestamp, "tested config timestamp")
    current = now.astimezone(timezone.utc)
    if tested_at > current + timedelta(minutes=1) or current - tested_at > MAX_PENDING_AGE:
        raise FinalizeError("pending receipt is not fresh")

    for key in (
        "instrument_sha256",
        "deployment_manifest_sha256",
        "source_attestation_sha256",
        "config_override_sha256",
    ):
        value = receipt.get(key)
        if not isinstance(value, str) or not e2e.HASH_RE.fullmatch(value):
            raise FinalizeError("pending receipt contains an invalid digest")
    asset_rows = receipt.get("asset_attestations")
    if (
        not isinstance(asset_rows, list)
        or len(asset_rows) != len(e2e.REMOTE_FILES)
        or any(
            not isinstance(row, dict)
            or set(row) != {"asset", "sha256"}
            or not isinstance(row.get("asset"), str)
            or not isinstance(row.get("sha256"), str)
            or not e2e.HASH_RE.fullmatch(row["sha256"])
            for row in asset_rows
        )
    ):
        raise FinalizeError("pending receipt asset hashes are invalid")
    asset_hashes = {
        row["asset"]: row["sha256"]
        for row in asset_rows
    }
    if set(asset_hashes) != set(e2e.REMOTE_FILES):
        raise FinalizeError("pending receipt asset hashes are invalid")
    if receipt.get("browser_family") not in {"chrome", "edge"}:
        raise FinalizeError("pending receipt browser identity is invalid")
    browser_version = receipt.get("browser_version")
    if (
        not isinstance(browser_version, str)
        or not 1 <= len(browser_version) <= 200
        or any(ord(character) < 32 for character in browser_version)
    ):
        raise FinalizeError("pending receipt browser identity is invalid")

    expected_values = {
        "config_intercept_count": 1,
        "browser_asset_count": len(e2e.BROWSER_ASSETS),
        "assignment_count": 12,
        "initial_submit_http_status": 200,
        "initial_submit_ok": True,
        "same_retry_http_status": 200,
        "same_retry_idempotent": True,
        "changed_retry_http_status": 409,
        "changed_retry_rejected": True,
        "fragment_removed": True,
        "private_inputs_cleared": True,
        "draft_cleared": True,
        "javascript_exception_count": 0,
        "database_record_count": 1,
    }
    if any(receipt.get(key) != value for key, value in expected_values.items()):
        raise FinalizeError("pending receipt checks are incomplete")
    if receipt.get("auto_cleanup_result") not in {
        "CLOSED_AND_REVOKED",
        "AUTO_CLOSE_FAILED",
    }:
        raise FinalizeError("pending receipt cleanup result is invalid")
    return tested_at


def attest_current_release(
    repository_root: Path,
    receipt: Mapping[str, Any],
    tested_at: datetime,
) -> dict[str, Any]:
    remote = e2e.fetch_remote_assets()
    release = e2e.validate_remote_release(
        remote,
        e2e.local_assets(repository_root),
        tested_at,
    )
    if (
        release.get("override_timestamp")
        != receipt.get("tested_config_timestamp")
        or e2e.asset_attestations(release.get("asset_hashes", {}))
        != receipt.get("asset_attestations")
        or e2e._sha256(release["config_override"])
        != receipt.get("config_override_sha256")
        or release["manifest"].get("deployment_manifest_sha256")
        != receipt.get("deployment_manifest_sha256")
        or release["instrument"].get("instrument_sha256")
        != receipt.get("instrument_sha256")
        or release["instrument"].get("hosted_version")
        != receipt.get("hosted_version")
    ):
        raise FinalizeError("pending receipt does not match the current staged release")
    return release


def read_cleanup_state(
    repository_root: Path,
    instrument_sha256: str,
    invite_id: str,
) -> dict[str, object]:
    from supabase.admin import manage_fielding_gate

    connection = None
    try:
        project_ref = manage_fielding_gate.resolve_project_ref(
            repository_root, None
        )
        database_url = manage_fielding_gate.required_database_url(
            os.environ, project_ref
        )
        connection = manage_fielding_gate.connect_database(database_url)
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute(
                    "set transaction isolation level repeatable read, read only"
                )
                cursor.execute(
                    """
                    select fielding_open
                    from research.pilot_instruments
                    where instrument_sha256 = %s
                    """,
                    (instrument_sha256,),
                )
                instrument = cursor.fetchone()
                cursor.execute(
                    """
                    select
                      count(*)::integer,
                      count(*) filter (where revoked_at is null)::integer
                    from private.pilot_invites
                    where invite_id = %s::uuid
                      and instrument_sha256 = %s
                    """,
                    (invite_id, instrument_sha256),
                )
                invitation = cursor.fetchone()
                cursor.execute(
                    """
                    select
                      (select count(*)::integer
                         from private.participant_identity
                         where invite_id = %s::uuid),
                      (select count(*)::integer
                         from research.pilot_submissions
                         where invite_id = %s::uuid),
                      (select count(*)::integer
                         from research.pilot_responses r
                         join research.pilot_submissions s
                           on s.submission_id = r.submission_id
                         where s.invite_id = %s::uuid)
                    """,
                    (invite_id, invite_id, invite_id),
                )
                data_counts = cursor.fetchone()
        if (
            instrument is None
            or invitation is None
            or data_counts is None
            or len(instrument) != 1
            or len(invitation) != 2
            or len(data_counts) != 3
        ):
            raise FinalizeError("cleanup verification returned an invalid shape")
        return {
            "fielding_open": bool(instrument[0]),
            "invite_count": int(invitation[0]),
            "unrevoked_invite_count": int(invitation[1]),
            "identity_count": int(data_counts[0]),
            "submission_count": int(data_counts[1]),
            "response_count": int(data_counts[2]),
        }
    except FinalizeError:
        raise
    except Exception as exc:
        raise FinalizeError("read-only cleanup verification failed") from exc
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def validate_cleanup_state(state: Mapping[str, object]) -> None:
    if (
        state.get("fielding_open") is not False
        or state.get("invite_count") != 0
        or state.get("unrevoked_invite_count") != 0
        or state.get("identity_count") != 0
        or state.get("submission_count") != 0
        or state.get("response_count") != 0
    ):
        raise FinalizeError(
            "cleanup is incomplete; fielding must be closed and target rows absent"
        )


def _load_inputs(
    repository_root: Path,
    private_root_arg: Path | None,
    pending_arg: Path | None,
    invite_arg: Path | None,
    output_arg: Path | None,
    *,
    now: datetime,
) -> tuple[dict[str, Any], dict[str, str], Path, datetime]:
    admin = repository_root / "supabase" / "admin"
    if str(admin) not in sys.path:
        sys.path.insert(0, str(admin))
    from private_storage import ensure_private_root, private_child

    root = ensure_private_root(repository_root, private_root_arg)
    pending_path = private_child(
        pending_arg or root / e2e.DEFAULT_RECEIPT_FILE,
        repository_root,
        root,
        must_exist=True,
    )
    invite_path = private_child(
        invite_arg
        or root / e2e.DEFAULT_INVITE_DIRECTORY / "invite_links.private.json",
        repository_root,
        root,
        must_exist=True,
    )
    output_path = private_child(
        output_arg or root / DEFAULT_FINAL_RECEIPT,
        repository_root,
        root,
        must_exist=False,
    )
    receipt = e2e._load_json(pending_path, "pending receipt")
    tested_at = validate_pending_receipt(receipt, now=now)
    invite_batch, invite_bytes = e2e._load_json_bytes(
        invite_path, "invite file"
    )
    invite = e2e.validate_disposable_invite_batch(
        invite_batch,
        at=tested_at,
    )
    provision_hash = e2e._sha256(invite_bytes)
    if not hmac.compare_digest(
        provision_hash, str(receipt.get("source_attestation_sha256"))
    ):
        raise FinalizeError("pending receipt does not match the provision file")
    if invite["instrument_sha256"] != receipt.get("instrument_sha256"):
        raise FinalizeError("pending receipt instrument does not match provision")
    return receipt, invite, output_path, tested_at


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    now = datetime.now(timezone.utc)
    try:
        receipt, invite, output_path, tested_at = _load_inputs(
            REPOSITORY_ROOT,
            args.private_root,
            args.pending_receipt,
            args.invite_file,
            args.output,
            now=now,
        )
        release = attest_current_release(
            REPOSITORY_ROOT, receipt, tested_at
        )
        cleanup = read_cleanup_state(
            REPOSITORY_ROOT,
            invite["instrument_sha256"],
            invite["invite_id"],
        )
        validate_cleanup_state(cleanup)
        final_receipt = {
            "schema_version": "1.0",
            "status": FINAL_STATUS,
            "tested_config_timestamp": receipt["tested_config_timestamp"],
            "finalized_at_utc": e2e._utc_seconds(now),
            "site_url": e2e.SITE_URL,
            "api_url": e2e.API_URL,
            "browser_family": receipt["browser_family"],
            "browser_version": receipt["browser_version"],
            "hosted_version": receipt["hosted_version"],
            "instrument_sha256": receipt["instrument_sha256"],
            "deployment_manifest_sha256": receipt[
                "deployment_manifest_sha256"
            ],
            "asset_attestations": e2e.asset_attestations(
                release["asset_hashes"]
            ),
            "config_override_sha256": receipt["config_override_sha256"],
            "published_hold_preserved": True,
            "cleanup_verified": True,
            "database_zero_verified": True,
            "cleanup_required": False,
            "fielding_authorized": False,
        }
        e2e.write_receipt(output_path, final_receipt)
    except (e2e.E2EError, FinalizeError, FileExistsError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(
            f"ERROR: E2E finalization failed ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 1
    print(
        "PASS: E2E cleanup verified; non-PII clean receipt written outside repository."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
