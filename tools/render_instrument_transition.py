#!/usr/bin/env python3
"""Render the one-time append-only hosted-instrument transition migration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from build_public_instrument import RELEASE_SOURCE_PATHS


PREVIOUS_INSTRUMENT_SHA256 = (
    "4a07da2785bb2228787f2dd4e57339bd5c132111d693681a12cb62baf64978e7"
)
PREVIOUS_HOSTED_VERSION = "v260903-pilot-hosted-1"
NEXT_HOSTED_VERSION = "v260903-pilot-hosted-2"
PREVIOUS_RESEARCH_BASIS_SHA256 = (
    "caaf49aab8d37955bca827e8b82df93ba55aa1cd12227de9e7ea74d1ef112c6b"
)
SOURCE_INSTRUMENT_SHA256 = (
    "b594a196eb7be720e57d974f4b5c6e4437b697e6ae20f01013e830af35707a51"
)
PREVIOUS_INSTRUMENT_RELATIVE = Path(
    "supabase/instrument_history/v260903-pilot-hosted-1.instrument.json"
)
BASELINE_SEED_RELATIVE = Path(
    "supabase/migrations/202609030002_seed_pilot_instrument.sql"
)
BASELINE_SEED_SHA256 = (
    "c0a8116c0bc8551a4ac0fb77bf776385bc002ad6f26d8b575e83adbac353ebd2"
)
CURRENT_INSTRUMENT_RELATIVE = Path("docs/instrument.json")
TRANSITION_MIGRATION_RELATIVE = Path(
    "supabase/migrations/202609030005_activate_hosted_instrument_v2.sql"
)
EXPECTED_ASSIGNMENT_CODES = tuple(f"PILOT_R{number:02d}" for number in range(1, 6))
EXPECTED_ITEMS_PER_ASSIGNMENT = 12
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,96}$")
PLACEHOLDER_RE = re.compile(
    r"__[A-Z][A-Z0-9_]*__|\b(?:PLACEHOLDER|TODO|TBD)\b",
    re.IGNORECASE,
)
ROW_KEYS = {
    "assignment_id",
    "pilot_rater_id",
    "display_position",
    "pilot_item_id",
    "sentence_text",
}
MUTABLE_TABLES = (
    "private.pilot_invites",
    "private.participant_identity",
    "research.pilot_submissions",
    "research.pilot_responses",
)
H1_CLOSED_LIFECYCLE_PREDICATE = """(
  (
    fielding_opened_at is null
    and fielding_closed_at is null
  )
  or
  (
    fielding_opened_at is not null
    and fielding_closed_at is not null
    and fielding_closed_at >= fielding_opened_at
  )
)"""
PREVIOUS_BASELINE_KEYS = {
    "schema_version",
    "instrument_sha256",
    "hosted_version",
    "source_offline_instrument_sha256",
    "items_per_participant",
    "item_count",
    "assignment_set_count",
    "assignment_count",
    "research_basis_sha256",
}


class DuplicateJsonKey(ValueError):
    pass


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKey(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, DuplicateJsonKey) as exc:
        raise ValueError("cannot read a unique-key UTF-8 instrument JSON") from exc
    if not isinstance(loaded, dict):
        raise ValueError("instrument JSON root must be an object")
    return loaded


def validate_baseline_seed(repository_root: Path) -> None:
    baseline_path = repository_root / BASELINE_SEED_RELATIVE
    try:
        baseline_bytes = baseline_path.read_bytes()
    except OSError as exc:
        raise ValueError("immutable baseline migration 002 is unavailable") from exc
    if sha256_bytes(baseline_bytes) != BASELINE_SEED_SHA256:
        raise ValueError("immutable baseline migration 002 hash mismatch")


def _validated_instrument(
    instrument: Mapping[str, Any],
    *,
    expected_version: str,
) -> dict[str, Any]:
    if not isinstance(instrument, Mapping):
        raise ValueError("instrument must be one JSON object")
    serialized = json.dumps(instrument, ensure_ascii=False)
    if PLACEHOLDER_RE.search(serialized):
        raise ValueError("instrument contains a forbidden placeholder")

    declared_hash = instrument.get("instrument_sha256")
    if not isinstance(declared_hash, str) or not HASH_RE.fullmatch(declared_hash):
        raise ValueError("instrument_sha256 is invalid")
    basis = dict(instrument)
    del basis["instrument_sha256"]
    calculated_hash = sha256_bytes(canonical_json(basis))
    if declared_hash != calculated_hash:
        raise ValueError("instrument hash mismatch")
    if instrument.get("schema_version") != "1.0":
        raise ValueError("instrument schema_version is invalid")
    if instrument.get("hosted_version") != expected_version:
        raise ValueError("instrument hosted_version is invalid")
    if instrument.get("source_offline_instrument_sha256") != SOURCE_INSTRUMENT_SHA256:
        raise ValueError("instrument source hash is invalid")
    if (
        instrument.get("dataset_role") != "synthetic_usability_pilot"
        or instrument.get("excluded_from_analysis") is not True
        or instrument.get("analysis_exclusion_reason")
        != "synthetic_usability_only_never_analysis"
        or instrument.get("items_per_participant") != EXPECTED_ITEMS_PER_ASSIGNMENT
    ):
        raise ValueError("instrument exclusion or response-count contract changed")

    release_hashes = instrument.get("release_source_hashes")
    if (
        not isinstance(release_hashes, dict)
        or any(
            not isinstance(key, str)
            or not isinstance(value, str)
            or not HASH_RE.fullmatch(value)
            for key, value in release_hashes.items()
        )
    ):
        raise ValueError("instrument release_source_hashes are invalid")

    assignments = instrument.get("assignments")
    if not isinstance(assignments, dict) or tuple(sorted(assignments)) != EXPECTED_ASSIGNMENT_CODES:
        raise ValueError("assignments must be exactly PILOT_R01 through PILOT_R05")
    assignment_ids: set[str] = set()
    item_text: dict[str, str] = {}
    item_counts: Counter[str] = Counter()
    normalized: dict[str, list[dict[str, Any]]] = {}
    for assignment_code in EXPECTED_ASSIGNMENT_CODES:
        rows = assignments[assignment_code]
        if not isinstance(rows, list) or len(rows) != EXPECTED_ITEMS_PER_ASSIGNMENT:
            raise ValueError(f"{assignment_code} must contain exactly 12 rows")
        positions: set[int] = set()
        assignment_items: set[str] = set()
        normalized_rows: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or set(row) != ROW_KEYS:
                raise ValueError(f"{assignment_code}[{index}] has invalid fields")
            assignment_id = row["assignment_id"]
            rater_id = row["pilot_rater_id"]
            position = row["display_position"]
            item_id = row["pilot_item_id"]
            sentence = row["sentence_text"]
            if not isinstance(assignment_id, str) or not ID_RE.fullmatch(assignment_id):
                raise ValueError("assignment_id is invalid")
            if assignment_id in assignment_ids:
                raise ValueError("assignment_id is duplicated")
            assignment_ids.add(assignment_id)
            if rater_id != assignment_code:
                raise ValueError("pilot_rater_id does not match its assignment")
            if (
                isinstance(position, bool)
                or not isinstance(position, int)
                or not 1 <= position <= EXPECTED_ITEMS_PER_ASSIGNMENT
                or position in positions
            ):
                raise ValueError("display_position is invalid or duplicated")
            positions.add(position)
            if (
                not isinstance(item_id, str)
                or not ID_RE.fullmatch(item_id)
                or item_id in assignment_items
            ):
                raise ValueError("pilot_item_id is invalid or duplicated")
            assignment_items.add(item_id)
            if (
                not isinstance(sentence, str)
                or not 1 <= len(sentence) <= 10000
                or "\x00" in sentence
                or PLACEHOLDER_RE.search(sentence)
            ):
                raise ValueError("sentence_text is invalid")
            if item_id in item_text and item_text[item_id] != sentence:
                raise ValueError("sentence_text differs across assignments")
            item_text[item_id] = sentence
            item_counts[item_id] += 1
            normalized_rows.append(dict(row))
        normalized_rows.sort(key=lambda value: value["display_position"])
        if [row["display_position"] for row in normalized_rows] != list(range(1, 13)):
            raise ValueError("assignment positions are not exactly 1 through 12")
        normalized[assignment_code] = normalized_rows
    if len(assignment_ids) != 60 or len(item_text) != 12:
        raise ValueError("instrument assignment shape is invalid")
    if set(item_counts.values()) != {5}:
        raise ValueError("every item must occur once in each assignment")
    return {
        "instrument": dict(instrument),
        "instrument_sha256": declared_hash,
        "instrument_version": expected_version,
        "items": item_text,
        "assignments": normalized,
    }


def _research_basis(instrument: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(instrument)
    for key in ("instrument_sha256", "hosted_version", "release_source_hashes"):
        result.pop(key, None)
    return result


def _validated_previous_baseline(previous: Mapping[str, Any]) -> dict[str, Any]:
    if set(previous) != PREVIOUS_BASELINE_KEYS:
        raise ValueError("previous instrument baseline fields are invalid")
    expected = {
        "schema_version": "1.0",
        "instrument_sha256": PREVIOUS_INSTRUMENT_SHA256,
        "hosted_version": PREVIOUS_HOSTED_VERSION,
        "source_offline_instrument_sha256": SOURCE_INSTRUMENT_SHA256,
        "items_per_participant": 12,
        "item_count": 12,
        "assignment_set_count": 5,
        "assignment_count": 60,
        "research_basis_sha256": PREVIOUS_RESEARCH_BASIS_SHA256,
    }
    if dict(previous) != expected:
        raise ValueError("previous instrument baseline is not the immutable v1 commitment")
    return {
        "instrument_sha256": PREVIOUS_INSTRUMENT_SHA256,
        "instrument_version": PREVIOUS_HOSTED_VERSION,
    }


def validate_transition(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    repository_root: Path,
) -> dict[str, Any]:
    validate_baseline_seed(repository_root)
    old = _validated_previous_baseline(previous)
    new = _validated_instrument(current, expected_version=NEXT_HOSTED_VERSION)
    if new["instrument_sha256"] == old["instrument_sha256"]:
        raise ValueError("current instrument hash did not change")
    current_research_hash = sha256_bytes(canonical_json(_research_basis(current)))
    if current_research_hash != PREVIOUS_RESEARCH_BASIS_SHA256:
        raise ValueError("research instrument content changed across the release transition")

    expected_paths = {
        name: repository_root / relative
        for name, relative in RELEASE_SOURCE_PATHS.items()
    }
    release_hashes = current.get("release_source_hashes")
    if set(release_hashes) != set(expected_paths):
        raise ValueError("current instrument release-source key set is invalid")
    try:
        current_hashes = {
            name: sha256_bytes(path.read_bytes())
            for name, path in expected_paths.items()
        }
    except OSError as exc:
        raise ValueError("cannot read a required current release source") from exc
    if release_hashes != current_hashes:
        raise ValueError("current instrument release-source hashes are stale")
    resolved_transition = (repository_root / TRANSITION_MIGRATION_RELATIVE).resolve()
    if any(path.resolve() == resolved_transition for path in expected_paths.values()):
        raise ValueError("transition migration must remain outside the instrument hash")
    return {"previous": old, "current": new}


def sql_text(value: str) -> str:
    if "\x00" in value:
        raise ValueError("PostgreSQL text cannot contain NUL")
    return "'" + value.replace("'", "''") + "'"


def baseline_semantic_contract(
    normalized_instrument: Mapping[str, Any],
) -> dict[str, tuple[tuple[object, ...], ...]]:
    """Return the stable research rows that migration 002 must have deployed."""

    items = tuple(
        sorted(
            (item_id, sentence)
            for item_id, sentence in normalized_instrument["items"].items()
        )
    )
    assignment_sets = tuple(
        (code, EXPECTED_ITEMS_PER_ASSIGNMENT)
        for code in EXPECTED_ASSIGNMENT_CODES
    )
    assignments = tuple(
        (
            code,
            row["display_position"],
            row["pilot_item_id"],
            row["assignment_id"],
        )
        for code in EXPECTED_ASSIGNMENT_CODES
        for row in normalized_instrument["assignments"][code]
    )
    return {
        "items": items,
        "assignment_sets": assignment_sets,
        "assignments": assignments,
    }


def render_sql(
    validated: Mapping[str, Any],
    previous_source: Path = PREVIOUS_INSTRUMENT_RELATIVE,
    current_source: Path = CURRENT_INSTRUMENT_RELATIVE,
) -> str:
    old = validated["previous"]
    new = validated["current"]
    old_hash = old["instrument_sha256"]
    new_hash = new["instrument_sha256"]
    old_version = old["instrument_version"]
    new_version = new["instrument_version"]
    baseline_contract = baseline_semantic_contract(new)
    baseline_item_rows = [
        "      ("
        + ", ".join((sql_text(item_id), sql_text(sentence), "true"))
        + ")"
        for item_id, sentence in baseline_contract["items"]
    ]
    baseline_set_rows = [
        "      ("
        + ", ".join((sql_text(code), f"{expected_count}::smallint", "true"))
        + ")"
        for code, expected_count in baseline_contract["assignment_sets"]
    ]
    baseline_assignment_rows = [
        "      ("
        + ", ".join(
            (
                sql_text(code),
                f"{position}::smallint",
                sql_text(item_id),
                sql_text(assignment_id),
            )
        )
        + ")"
        for code, position, item_id, assignment_id in baseline_contract["assignments"]
    ]
    item_rows = [
        f"  ({sql_text(new_hash)}, {sql_text(item_id)}, {sql_text(sentence)})"
        for item_id, sentence in sorted(new["items"].items())
    ]
    set_rows = [
        f"  ({sql_text(new_hash)}, {sql_text(code)}, 12)"
        for code in EXPECTED_ASSIGNMENT_CODES
    ]
    assignment_rows: list[str] = []
    for code in EXPECTED_ASSIGNMENT_CODES:
        for row in new["assignments"][code]:
            assignment_rows.append(
                "  ("
                + ", ".join(
                    (
                        sql_text(new_hash),
                        sql_text(code),
                        str(row["display_position"]),
                        sql_text(row["pilot_item_id"]),
                        sql_text(row["assignment_id"]),
                    )
                )
                + ")"
            )
    zero_guard = "\n     or ".join(
        f"(select count(*) from {table}) <> 0" for table in MUTABLE_TABLES
    )
    zero_postcheck = zero_guard
    h1_lifecycle_precheck = H1_CLOSED_LIFECYCLE_PREDICATE.replace(
        "\n", "\n      "
    )
    h1_lifecycle_postcheck = H1_CLOSED_LIFECYCLE_PREDICATE.replace(
        "\n", "\n          "
    )
    return f"""-- Generated by tools/render_instrument_transition.py.
-- Previous source: {previous_source.as_posix()}
-- Current source: {current_source.as_posix()}
-- One-time append-only instrument transition; apply only after migration 004.
begin;
set transaction isolation level serializable;
set local standard_conforming_strings = on;

lock table
  research.pilot_instruments,
  research.pilot_items,
  research.pilot_assignment_sets,
  research.pilot_assignments,
  private.pilot_invites,
  private.participant_identity,
  research.pilot_submissions,
  research.pilot_responses
in access exclusive mode;

create temporary table _pilot_expected_invite_004_contract (
  token_hmac bytea not null,
  instrument_sha256 text not null,
  assignment_code text not null,
  revoked_at timestamptz,
  used_at timestamptz,
  invite_purpose text not null,
  admin_id_hmac bytea,
  constraint _pilot_expected_invite_purpose_ck check (
    invite_purpose in ('participant', 'disposable_e2e', 'pi_manual_test')
  ),
  constraint _pilot_expected_invite_admin_auth_ck check (
    (
      invite_purpose = 'pi_manual_test'
      and admin_id_hmac is not null
      and octet_length(admin_id_hmac) = 32
      and admin_id_hmac <> token_hmac
      and assignment_code = 'PILOT_R01'
    )
    or
    (
      invite_purpose in ('participant', 'disposable_e2e')
      and admin_id_hmac is null
    )
  )
) on commit drop;

create unique index _pilot_expected_admin_id_hmac_idx
  on _pilot_expected_invite_004_contract (admin_id_hmac)
  where admin_id_hmac is not null;

create unique index _pilot_expected_one_outstanding_pi_manual_test_idx
  on _pilot_expected_invite_004_contract (instrument_sha256)
  where invite_purpose = 'pi_manual_test'
    and revoked_at is null
    and used_at is null;

create temporary table _pilot_expected_submission_004_contract (
  dataset_role text not null,
  excluded_from_analysis boolean not null,
  analysis_exclusion_reason text not null,
  constraint _pilot_expected_submission_role_ck check (
    (
      dataset_role = 'synthetic_usability_pilot'
      and excluded_from_analysis
      and analysis_exclusion_reason = 'synthetic_usability_only_never_analysis'
    )
    or
    (
      dataset_role = 'synthetic_pi_manual_test'
      and excluded_from_analysis
      and analysis_exclusion_reason = 'pi_manual_test_never_analysis'
    )
  )
) on commit drop;

do $instrument_transition_precheck$
begin
  if to_regclass('private.pilot_withdrawal_events') is null then
    raise exception 'instrument transition requires the expected migration 003 schema';
  end if;
  if (
    select count(*)
    from pg_attribute
    where attrelid = 'private.pilot_invites'::regclass
      and attname in ('invite_purpose', 'admin_id_hmac')
      and attnum > 0
      and not attisdropped
  ) <> 2
     or not exists (
       select 1
       from pg_attribute
       where attrelid = 'private.pilot_invites'::regclass
         and attname = 'invite_purpose'
         and atttypid = 'text'::regtype
         and attnotnull
         and not atthasdef
         and attidentity = ''
         and attgenerated = ''
     )
     or not exists (
       select 1
       from pg_attribute
       where attrelid = 'private.pilot_invites'::regclass
         and attname = 'admin_id_hmac'
         and atttypid = 'bytea'::regtype
         and not attnotnull
         and not atthasdef
         and attidentity = ''
         and attgenerated = ''
     ) then
    raise exception 'instrument transition requires exact migration 004 columns';
  end if;
  if not exists (
       select 1
       from pg_constraint actual
       where actual.conrelid = 'private.pilot_invites'::regclass
         and actual.conname = 'pilot_invites_purpose_ck'
         and actual.contype = 'c'
         and actual.convalidated
         and not actual.connoinherit
         and pg_get_expr(actual.conbin, actual.conrelid, true) = (
           select pg_get_expr(expected.conbin, expected.conrelid, true)
           from pg_constraint expected
           where expected.conrelid =
             'pg_temp._pilot_expected_invite_004_contract'::regclass
             and expected.conname = '_pilot_expected_invite_purpose_ck'
         )
     )
     or not exists (
       select 1
       from pg_constraint actual
       where actual.conrelid = 'private.pilot_invites'::regclass
         and actual.conname = 'pilot_invites_admin_auth_ck'
         and actual.contype = 'c'
         and actual.convalidated
         and not actual.connoinherit
         and pg_get_expr(actual.conbin, actual.conrelid, true) = (
           select pg_get_expr(expected.conbin, expected.conrelid, true)
           from pg_constraint expected
           where expected.conrelid =
             'pg_temp._pilot_expected_invite_004_contract'::regclass
             and expected.conname = '_pilot_expected_invite_admin_auth_ck'
         )
     )
     or not exists (
       select 1
       from pg_constraint actual
       where actual.conrelid = 'research.pilot_submissions'::regclass
         and actual.conname = 'pilot_submissions_role_ck'
         and actual.contype = 'c'
         and actual.convalidated
         and not actual.connoinherit
         and pg_get_expr(actual.conbin, actual.conrelid, true) = (
           select pg_get_expr(expected.conbin, expected.conrelid, true)
           from pg_constraint expected
           where expected.conrelid =
             'pg_temp._pilot_expected_submission_004_contract'::regclass
             and expected.conname = '_pilot_expected_submission_role_ck'
         )
     ) then
    raise exception 'instrument transition requires exact migration 004 checks';
  end if;
  if not exists (
       select 1
       from pg_index actual
       join pg_class actual_index on actual_index.oid = actual.indexrelid
       where actual.indrelid = 'private.pilot_invites'::regclass
         and actual_index.relname = 'pilot_invites_admin_id_hmac_idx'
         and actual.indisunique
         and actual.indisvalid
         and actual.indisready
         and actual.indislive
         and not actual.indisprimary
         and not actual.indisexclusion
         and actual.indnkeyatts = 1
         and actual.indnatts = 1
         and pg_get_indexdef(actual.indexrelid, 1, true) = (
           select pg_get_indexdef(expected.indexrelid, 1, true)
           from pg_index expected
           join pg_class expected_index
             on expected_index.oid = expected.indexrelid
           where expected.indrelid =
             'pg_temp._pilot_expected_invite_004_contract'::regclass
             and expected_index.relname = '_pilot_expected_admin_id_hmac_idx'
         )
         and pg_get_expr(actual.indpred, actual.indrelid, true) = (
           select pg_get_expr(expected.indpred, expected.indrelid, true)
           from pg_index expected
           join pg_class expected_index
             on expected_index.oid = expected.indexrelid
           where expected.indrelid =
             'pg_temp._pilot_expected_invite_004_contract'::regclass
             and expected_index.relname = '_pilot_expected_admin_id_hmac_idx'
         )
     )
     or not exists (
       select 1
       from pg_index actual
       join pg_class actual_index on actual_index.oid = actual.indexrelid
       where actual.indrelid = 'private.pilot_invites'::regclass
         and actual_index.relname =
           'pilot_invites_one_outstanding_pi_manual_test_idx'
         and actual.indisunique
         and actual.indisvalid
         and actual.indisready
         and actual.indislive
         and not actual.indisprimary
         and not actual.indisexclusion
         and actual.indnkeyatts = 1
         and actual.indnatts = 1
         and pg_get_indexdef(actual.indexrelid, 1, true) = (
           select pg_get_indexdef(expected.indexrelid, 1, true)
           from pg_index expected
           join pg_class expected_index
             on expected_index.oid = expected.indexrelid
           where expected.indrelid =
             'pg_temp._pilot_expected_invite_004_contract'::regclass
             and expected_index.relname =
               '_pilot_expected_one_outstanding_pi_manual_test_idx'
         )
         and pg_get_expr(actual.indpred, actual.indrelid, true) = (
           select pg_get_expr(expected.indpred, expected.indrelid, true)
           from pg_index expected
           join pg_class expected_index
             on expected_index.oid = expected.indexrelid
           where expected.indrelid =
             'pg_temp._pilot_expected_invite_004_contract'::regclass
             and expected_index.relname =
               '_pilot_expected_one_outstanding_pi_manual_test_idx'
         )
     ) then
    raise exception 'instrument transition requires exact migration 004 indexes';
  end if;
  if (select count(*) from research.pilot_instruments) <> 1 then
    raise exception 'instrument transition requires exactly one baseline instrument';
  end if;
  if not exists (
    select 1
    from research.pilot_instruments
    where instrument_sha256 = {sql_text(old_hash)}
      and instrument_version = {sql_text(old_version)}
      and source_offline_sha256 = {sql_text(SOURCE_INSTRUMENT_SHA256)}
      and expected_response_count = 12
      and is_active is true
      and fielding_open is false
      and created_at is not null
      and activated_at = created_at
      and activated_at is not null
      and {h1_lifecycle_precheck}
  ) then
    raise exception 'instrument transition baseline state is invalid';
  end if;
  if exists (
    select 1 from research.pilot_instruments
    where instrument_sha256 = {sql_text(new_hash)}
       or instrument_version = {sql_text(new_version)}
  ) then
    raise exception 'instrument transition target already exists';
  end if;
  if (select count(*) from research.pilot_items where instrument_sha256 = {sql_text(old_hash)}) <> 12
     or (select count(*) from research.pilot_assignment_sets where instrument_sha256 = {sql_text(old_hash)}) <> 5
     or (
       select count(*)
       from research.pilot_assignment_sets
       where instrument_sha256 = {sql_text(old_hash)}
         and assignment_code in ('PILOT_R01', 'PILOT_R02', 'PILOT_R03', 'PILOT_R04', 'PILOT_R05')
         and expected_item_count = 12
     ) <> 5
     or (select count(*) from research.pilot_assignments where instrument_sha256 = {sql_text(old_hash)}) <> 60
     or exists (
       select 1
       from research.pilot_assignments
       where instrument_sha256 = {sql_text(old_hash)}
       group by assignment_code
       having count(*) <> 12
          or count(distinct display_position) <> 12
          or count(distinct pilot_item_id) <> 12
          or min(display_position) <> 1
          or max(display_position) <> 12
  ) then
    raise exception 'instrument transition baseline shape is invalid';
  end if;
  if exists (
    with expected_h1_items (
      pilot_item_id, sentence_text, created_with_instrument
    ) as (
      values
{",\n".join(baseline_item_rows)}
    ),
    actual_h1_items (
      pilot_item_id, sentence_text, created_with_instrument
    ) as (
      select
        item.pilot_item_id,
        item.sentence_text,
        item.created_at = instrument.created_at
      from research.pilot_items item
      join research.pilot_instruments instrument
        on instrument.instrument_sha256 = item.instrument_sha256
      where item.instrument_sha256 = {sql_text(old_hash)}
    ),
    actual_minus_expected as (
      select pilot_item_id, sentence_text, created_with_instrument
      from actual_h1_items
      except
      select pilot_item_id, sentence_text, created_with_instrument
      from expected_h1_items
    ),
    expected_minus_actual as (
      select pilot_item_id, sentence_text, created_with_instrument
      from expected_h1_items
      except
      select pilot_item_id, sentence_text, created_with_instrument
      from actual_h1_items
    )
    select 1 from actual_minus_expected
    union all
    select 1 from expected_minus_actual
  ) then
    raise exception 'instrument transition baseline item rows drifted';
  end if;
  if exists (
    with expected_h1_assignment_sets (
      assignment_code, expected_item_count, created_with_instrument
    ) as (
      values
{",\n".join(baseline_set_rows)}
    ),
    actual_h1_assignment_sets (
      assignment_code, expected_item_count, created_with_instrument
    ) as (
      select
        assignment_set.assignment_code,
        assignment_set.expected_item_count,
        assignment_set.created_at = instrument.created_at
      from research.pilot_assignment_sets assignment_set
      join research.pilot_instruments instrument
        on instrument.instrument_sha256 = assignment_set.instrument_sha256
      where assignment_set.instrument_sha256 = {sql_text(old_hash)}
    ),
    actual_minus_expected as (
      select assignment_code, expected_item_count, created_with_instrument
      from actual_h1_assignment_sets
      except
      select assignment_code, expected_item_count, created_with_instrument
      from expected_h1_assignment_sets
    ),
    expected_minus_actual as (
      select assignment_code, expected_item_count, created_with_instrument
      from expected_h1_assignment_sets
      except
      select assignment_code, expected_item_count, created_with_instrument
      from actual_h1_assignment_sets
    )
    select 1 from actual_minus_expected
    union all
    select 1 from expected_minus_actual
  ) then
    raise exception 'instrument transition baseline assignment-set rows drifted';
  end if;
  if exists (
    with expected_h1_assignments (
      assignment_code, display_position, pilot_item_id, assignment_id
    ) as (
      values
{",\n".join(baseline_assignment_rows)}
    ),
    actual_h1_assignments (
      assignment_code, display_position, pilot_item_id, assignment_id
    ) as (
      select
        assignment_code,
        display_position,
        pilot_item_id,
        assignment_id
      from research.pilot_assignments
      where instrument_sha256 = {sql_text(old_hash)}
    ),
    actual_minus_expected as (
      select assignment_code, display_position, pilot_item_id, assignment_id
      from actual_h1_assignments
      except
      select assignment_code, display_position, pilot_item_id, assignment_id
      from expected_h1_assignments
    ),
    expected_minus_actual as (
      select assignment_code, display_position, pilot_item_id, assignment_id
      from expected_h1_assignments
      except
      select assignment_code, display_position, pilot_item_id, assignment_id
      from actual_h1_assignments
    )
    select 1 from actual_minus_expected
    union all
    select 1 from expected_minus_actual
  ) then
    raise exception 'instrument transition baseline assignment rows drifted';
  end if;
  if {zero_guard} then
    raise exception 'instrument transition requires all mutable pilot tables to be empty';
  end if;
end;
$instrument_transition_precheck$;

insert into research.pilot_instruments (
  instrument_sha256,
  instrument_version,
  source_offline_sha256,
  expected_response_count,
  is_active,
  fielding_open
) values (
  {sql_text(new_hash)},
  {sql_text(new_version)},
  {sql_text(SOURCE_INSTRUMENT_SHA256)},
  12,
  false,
  false
);

insert into research.pilot_items (
  instrument_sha256, pilot_item_id, sentence_text
) values
{",\n".join(item_rows)};

insert into research.pilot_assignment_sets (
  instrument_sha256, assignment_code, expected_item_count
) values
{",\n".join(set_rows)};

insert into research.pilot_assignments (
  instrument_sha256, assignment_code, display_position, pilot_item_id, assignment_id
) values
{",\n".join(assignment_rows)};

do $instrument_transition_seed_check$
begin
  if (select count(*) from research.pilot_items where instrument_sha256 = {sql_text(new_hash)}) <> 12
     or (select count(*) from research.pilot_assignment_sets where instrument_sha256 = {sql_text(new_hash)}) <> 5
     or (select count(*) from research.pilot_assignments where instrument_sha256 = {sql_text(new_hash)}) <> 60
     or exists (
       select 1
       from research.pilot_assignments
       where instrument_sha256 = {sql_text(new_hash)}
       group by assignment_code
       having count(*) <> 12
          or count(distinct display_position) <> 12
          or count(distinct pilot_item_id) <> 12
          or min(display_position) <> 1
          or max(display_position) <> 12
     )
     or not exists (
       select 1 from research.pilot_instruments
       where instrument_sha256 = {sql_text(new_hash)}
         and instrument_version = {sql_text(new_version)}
         and source_offline_sha256 = {sql_text(SOURCE_INSTRUMENT_SHA256)}
         and expected_response_count = 12
         and is_active is false
         and fielding_open is false
         and activated_at is null
         and fielding_opened_at is null
         and fielding_closed_at is null
     ) then
    raise exception 'instrument transition target seed is invalid';
  end if;
end;
$instrument_transition_seed_check$;

do $instrument_transition_activation$
declare
  old_update_count integer;
  new_update_count integer;
begin
  update research.pilot_instruments
  set is_active = false
  where instrument_sha256 = {sql_text(old_hash)}
    and instrument_version = {sql_text(old_version)}
    and is_active is true
    and fielding_open is false;
  get diagnostics old_update_count = row_count;
  if old_update_count <> 1 then
    raise exception 'instrument transition baseline activation update failed';
  end if;

  update research.pilot_instruments
  set is_active = true,
      activated_at = statement_timestamp()
  where instrument_sha256 = {sql_text(new_hash)}
    and instrument_version = {sql_text(new_version)}
    and is_active is false
    and fielding_open is false
    and activated_at is null;
  get diagnostics new_update_count = row_count;
  if new_update_count <> 1 then
    raise exception 'instrument transition target activation update failed';
  end if;
end;
$instrument_transition_activation$;

do $instrument_transition_postcheck$
begin
  if (select count(*) from research.pilot_instruments) <> 2
     or (select count(*) from research.pilot_instruments where is_active is true) <> 1
     or not exists (
       select 1 from research.pilot_instruments
       where instrument_sha256 = {sql_text(old_hash)}
          and instrument_version = {sql_text(old_version)}
          and is_active is false
          and fielding_open is false
          and activated_at is not null
          and {h1_lifecycle_postcheck}
     )
     or not exists (
       select 1 from research.pilot_instruments
       where instrument_sha256 = {sql_text(new_hash)}
         and instrument_version = {sql_text(new_version)}
         and is_active is true
         and fielding_open is false
         and activated_at is not null
         and fielding_opened_at is null
         and fielding_closed_at is null
     ) then
    raise exception 'instrument transition activation postcondition failed';
  end if;
  if (select count(*) from research.pilot_items where instrument_sha256 = {sql_text(new_hash)}) <> 12
     or (select count(*) from research.pilot_assignment_sets where instrument_sha256 = {sql_text(new_hash)}) <> 5
     or (select count(*) from research.pilot_assignments where instrument_sha256 = {sql_text(new_hash)}) <> 60
     or {zero_postcheck} then
    raise exception 'instrument transition row-count postcondition failed';
  end if;
end;
$instrument_transition_postcheck$;

commit;
"""


def ensure_paths(
    previous_path: Path,
    current_path: Path,
    output_path: Path,
    repository_root: Path,
) -> tuple[Path, Path, Path]:
    expected_previous = (repository_root / PREVIOUS_INSTRUMENT_RELATIVE).resolve()
    expected_current = (repository_root / CURRENT_INSTRUMENT_RELATIVE).resolve()
    expected_output = (repository_root / TRANSITION_MIGRATION_RELATIVE).resolve()
    previous = previous_path.resolve()
    current = current_path.resolve()
    output = output_path.resolve()
    if previous != expected_previous or not previous.is_file():
        raise ValueError(f"previous instrument must be {expected_previous}")
    if current != expected_current or not current.is_file():
        raise ValueError(f"current instrument must be {expected_current}")
    if output != expected_output:
        raise ValueError(f"transition output must be {expected_output}")
    if os.path.lexists(output):
        raise FileExistsError(f"refusing to overwrite existing output: {output}")
    return previous, current, output


def render_to_path(
    previous_path: Path,
    current_path: Path,
    output_path: Path,
    repository_root: Path,
) -> dict[str, object]:
    previous, current, output = ensure_paths(
        previous_path, current_path, output_path, repository_root
    )
    validated = validate_transition(
        load_json(previous), load_json(current), repository_root
    )
    sql = render_sql(
        validated,
        PREVIOUS_INSTRUMENT_RELATIVE,
        CURRENT_INSTRUMENT_RELATIVE,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(sql)
    return {
        "previous_instrument_sha256": validated["previous"]["instrument_sha256"],
        "current_instrument_sha256": validated["current"]["instrument_sha256"],
        "items": len(validated["current"]["items"]),
        "assignment_sets": len(validated["current"]["assignments"]),
        "assignments": sum(
            len(rows) for rows in validated["current"]["assignments"].values()
        ),
    }


def parser() -> argparse.ArgumentParser:
    repository_root = Path(__file__).resolve().parents[1]
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--previous-instrument",
        type=Path,
        default=repository_root / PREVIOUS_INSTRUMENT_RELATIVE,
    )
    result.add_argument(
        "--current-instrument",
        type=Path,
        default=repository_root / CURRENT_INSTRUMENT_RELATIVE,
    )
    result.add_argument(
        "--output",
        type=Path,
        default=repository_root / TRANSITION_MIGRATION_RELATIVE,
    )
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    repository_root = Path(__file__).resolve().parents[1]
    try:
        counts = render_to_path(
            args.previous_instrument,
            args.current_instrument,
            args.output,
            repository_root,
        )
    except (ValueError, FileExistsError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(
        "Rendered append-only instrument transition "
        f"rows=items:{counts['items']},"
        f"assignment_sets:{counts['assignment_sets']},"
        f"assignments:{counts['assignments']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
