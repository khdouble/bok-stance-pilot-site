"""Shared fail-closed validation for PI-controlled live configuration."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone


REQUIRED_TEXT_FIELDS = (
    "privacyNoticeVersion",
    "dataController",
    "contactEmail",
    "retentionNotice",
    "retentionEndDate",
    "dataRegion",
    "ethicsDisposition",
    "ethicsReference",
    "withdrawalProcedureVersion",
    "remoteE2eVerifiedAt",
    "identityPurpose",
)
EXPECTED_IDENTITY_PURPOSE = "사전 지정 참여자의 응답자료 구별과 제출자료 확인"
ALLOWED_DATA_REGIONS = {
    "ap-northeast-1",
    "ap-northeast-2",
    "ap-south-1",
    "ap-southeast-1",
    "ap-southeast-2",
    "ap-southeast-3",
    "ap-southeast-4",
    "ca-central-1",
    "eu-central-1",
    "eu-central-2",
    "eu-north-1",
    "eu-south-1",
    "eu-south-2",
    "eu-west-1",
    "eu-west-2",
    "eu-west-3",
    "sa-east-1",
    "us-east-1",
    "us-east-2",
    "us-west-1",
    "us-west-2",
}
ALLOWED_ETHICS_DISPOSITIONS = {"approved", "exempt", "not_required"}
PLACEHOLDER_RE = re.compile(
    r"(?:PENDING|TBD|TODO|PLACEHOLDER|EXAMPLE|미정|추후|예시|테스트)",
    re.IGNORECASE,
)
VERSION_RE = re.compile(
    r"^consent-v(?P<date>\d{4}-\d{2}-\d{2})-r[1-9]\d*$"
)
WITHDRAWAL_RE = re.compile(
    r"^withdrawal-v(?P<date>\d{4}-\d{2}-\d{2})-r[1-9]\d*$"
)
EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
UTC_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
)


def quoted_value(config: str, key: str) -> str:
    pattern = r'^\s*' + re.escape(key) + r':\s*"([^"]*)"\s*,?\s*$'
    matches = re.findall(pattern, config, flags=re.MULTILINE)
    if len(matches) != 1:
        raise ValueError(f"site-config.js must define {key} exactly once")
    return matches[0]


def boolean_value(config: str, key: str) -> bool:
    pattern = r"^\s*" + re.escape(key) + r":\s*(true|false)\s*,?\s*$"
    matches = re.findall(pattern, config, flags=re.MULTILINE)
    if len(matches) != 1:
        raise ValueError(f"site-config.js must define {key} exactly once")
    return matches[0] == "true"


def parse_pi_values(config: str) -> dict[str, str]:
    return {key: quoted_value(config, key) for key in REQUIRED_TEXT_FIELDS}


def has_placeholder(config: str, privacy: str) -> bool:
    return bool(PLACEHOLDER_RE.search(config) or PLACEHOLDER_RE.search(privacy))


def _meaningful(value: str, minimum: int, maximum: int) -> bool:
    return (
        value == value.strip()
        and minimum <= len(value) <= maximum
        and not PLACEHOLDER_RE.search(value)
        and not any(ord(character) < 32 for character in value)
    )


def _calendar_date(value: str) -> date | None:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def validate_live_config(
    config: str,
    privacy: str,
    *,
    today: date | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Return every reason this configuration must not be used for fielding."""

    errors: list[str] = []
    try:
        values = parse_pi_values(config)
    except ValueError as error:
        return [str(error)]

    version = values["privacyNoticeVersion"]
    version_match = VERSION_RE.fullmatch(version)
    version_date = (
        _calendar_date(version_match.group("date"))
        if version_match is not None
        else None
    )
    if (
        version_match is None
        or version_date is None
    ):
        errors.append(
            "privacyNoticeVersion must match consent-vYYYY-MM-DD-rN"
        )

    controller = values["dataController"]
    if not _meaningful(controller, 2, 100) or not re.search(
        r"[A-Za-z가-힣]", controller
    ):
        errors.append("dataController is not a meaningful display name")

    email = values["contactEmail"]
    email_domain = email.rsplit("@", 1)[-1].lower()
    if (
        len(email) > 254
        or not EMAIL_RE.fullmatch(email)
        or email_domain.endswith((".invalid", ".example", ".test"))
        or email_domain in {"example.com", "localhost"}
    ):
        errors.append("contactEmail is not a deployable email address")

    end_date = _calendar_date(values["retentionEndDate"])
    check_today = today or datetime.now(timezone.utc).date()
    if end_date is None or end_date < check_today:
        errors.append("retentionEndDate must be a current/future ISO date")

    retention = values["retentionNotice"]
    if (
        not _meaningful(retention, 12, 300)
        or values["retentionEndDate"] not in retention
        or not re.search(r"파기|삭제", retention)
    ):
        errors.append(
            "retentionNotice must include retentionEndDate and deletion wording"
        )

    if values["dataRegion"] not in ALLOWED_DATA_REGIONS:
        errors.append("dataRegion is not a recognized Supabase region code")

    if values["ethicsDisposition"] not in ALLOWED_ETHICS_DISPOSITIONS:
        errors.append(
            "ethicsDisposition must be approved, exempt, or not_required"
        )
    ethics_prefix = values["ethicsDisposition"] + ":"
    ethics_suffix = (
        values["ethicsReference"][len(ethics_prefix):]
        if values["ethicsReference"].startswith(ethics_prefix)
        else ""
    )
    if (
        not _meaningful(ethics_suffix, 8, 160)
        or not re.search(r"[A-Za-z가-힣]", ethics_suffix)
        or not re.search(r"\d", ethics_suffix)
    ):
        errors.append(
            "ethicsReference must match disposition:meaningful dated record"
        )

    withdrawal_match = WITHDRAWAL_RE.fullmatch(
        values["withdrawalProcedureVersion"]
    )
    withdrawal_date = (
        _calendar_date(withdrawal_match.group("date"))
        if withdrawal_match is not None
        else None
    )
    if (
        withdrawal_match is None
        or withdrawal_date is None
    ):
        errors.append(
            "withdrawalProcedureVersion must match withdrawal-vYYYY-MM-DD-rN"
        )

    verified_at = values["remoteE2eVerifiedAt"]
    verified_datetime: datetime | None = None
    if UTC_TIMESTAMP_RE.fullmatch(verified_at):
        try:
            verified_datetime = datetime.strptime(
                verified_at, "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            verified_datetime = None
    check_now = now or datetime.now(timezone.utc)
    if verified_datetime is None or verified_datetime > check_now:
        errors.append("remoteE2eVerifiedAt must be a non-future UTC timestamp")
    elif (
        version_date is not None
        and withdrawal_date is not None
        and (
            verified_datetime.date() < version_date
            or verified_datetime.date() < withdrawal_date
        )
    ):
        errors.append(
            "remoteE2eVerifiedAt must follow consent and withdrawal versions"
        )

    if values["identityPurpose"] != EXPECTED_IDENTITY_PURPOSE:
        errors.append("identityPurpose changed from the approved narrow purpose")

    privacy_values = (
        "privacyNoticeVersion",
        "dataController",
        "contactEmail",
        "retentionEndDate",
        "retentionNotice",
        "dataRegion",
        "ethicsReference",
        "withdrawalProcedureVersion",
    )
    for key in privacy_values:
        if values[key] not in privacy:
            errors.append(f"privacy.html does not display {key}")

    return errors
