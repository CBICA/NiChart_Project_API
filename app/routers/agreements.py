"""
Agreement acceptance records — authenticated.

The upload agreement (anonymised data only, the user is responsible for their DUA, …)
used to be remembered only in the browser that accepted it, so there was no record of
who agreed to what, and when. The frontend now records each acceptance here as well.

  GET  /agreements   — this user's acceptances
  POST /agreements   — record an acceptance

Records are appended to ``{data_root}/_working/agreements/{user}.jsonl`` and also
written to the application log, which outlives an ephemeral local disk in cloud mode.
"""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.auth.dependencies import CurrentUser, require_auth
from app.config import Settings, get_settings
from app.models.errors import ErrorDetail

_log = logging.getLogger(__name__)

router = APIRouter(tags=["Agreements"])


class AgreementAcceptance(BaseModel):
    agreement: str = Field(
        pattern=r"^[a-z0-9_-]{1,40}$",
        description="Which agreement, e.g. 'upload'.",
    )
    version: str = Field(
        pattern=r"^[A-Za-z0-9._-]{1,40}$",
        description="The version of its text that was shown.",
    )


class AgreementRecord(AgreementAcceptance):
    accepted_at: datetime


class AgreementList(BaseModel):
    records: list[AgreementRecord] = Field(description="Oldest first.")


def _records_path(settings: Settings, user: CurrentUser) -> Path:
    # Cognito subs are UUIDs and the local user is LOCAL_USER; anything else is
    # reduced to a safe file name.
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", user.sub or "unknown") or "unknown"
    return Path(settings.data_root) / "_working" / "agreements" / f"{safe}.jsonl"


def _read(path: Path) -> list[AgreementRecord]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text().splitlines():
        try:
            records.append(AgreementRecord(**json.loads(line)))
        except Exception:
            continue  # a damaged line doesn't hide the others
    return records


_AUTH_ERRORS = {401: {"model": ErrorDetail, "description": "Not signed in."}}


@router.get(
    "/agreements",
    summary="List this user's agreement acceptances",
    response_model=AgreementList,
    responses=_AUTH_ERRORS,
)
async def list_agreements(
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> AgreementList:
    return AgreementList(records=_read(_records_path(settings, user)))


@router.post(
    "/agreements",
    summary="Record that this user accepted an agreement",
    description=(
        "Appends a record (agreement, version, time) for the signed-in user. Accepting "
        "the same version again adds another record; clients check ``GET /agreements`` "
        "first."
    ),
    status_code=201,
    response_model=AgreementRecord,
    responses=_AUTH_ERRORS,
)
async def record_agreement(
    body: AgreementAcceptance,
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> AgreementRecord:
    record = AgreementRecord(
        agreement=body.agreement,
        version=body.version,
        accepted_at=datetime.now(timezone.utc),
    )
    path = _records_path(settings, user)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(record.model_dump_json() + "\n")
    _log.info(
        "agreement accepted: user=%s agreement=%s version=%s at=%s",
        user.sub, record.agreement, record.version, record.accepted_at.isoformat(),
    )
    return record
