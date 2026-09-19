"""Regulator API. JWT-protected; EVERY call (including login and rejected calls) writes audit_log.
Prototype-grade auth: shared secret -> 15-minute JWT."""
import os
import uuid
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

import auth
import db
from ratelimit import ip_limit

router = APIRouter(prefix="/api/regulator", tags=["regulator"])
_bearer = HTTPBearer(auto_error=False)

REPORT_TRANSITIONS = {"open": {"under_investigation"}, "under_investigation": {"confirmed", "dismissed"}}
OPEN_DISPUTE = ("open", "under_review")


def sla_days() -> int:
    try:
        return max(0, int(os.environ.get("SLA_DAYS", "7")))
    except ValueError:
        return 7


def _audit(actor: str, action: str, target: str) -> None:
    db.run('INSERT INTO audit_log (id, actor, action, target, "timestamp") VALUES (gen_random_uuid(), %s, %s, %s, now())',
           (actor[:200], action[:200], target[:500]))


def _target(request: Request) -> str:
    q = request.url.query
    return request.url.path + (f"?{q}" if q else "")


def regulator_call(request: Request, creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)) -> str:
    """Authenticate + audit. Rejected attempts are audited too (actor 'anonymous')."""
    route = getattr(request.scope.get("route"), "path", request.url.path)
    action = f"regulator:{request.method} {route}"
    try:
        actor = auth.authenticate(creds)
    except HTTPException as e:
        _audit("anonymous", action + f" denied({e.status_code})", _target(request))
        raise
    _audit(actor, action, _target(request))
    return actor


class LoginBody(BaseModel):
    username: str = Field(max_length=200)
    secret: str = Field(max_length=500)


@router.post("/login")
def login(body: LoginBody, request: Request, _rl=Depends(ip_limit)):
    if not auth.configured():
        _audit("anonymous", "regulator:login unavailable", "login")
        raise HTTPException(503, "Regulator access is not configured")
    if not auth.check_credentials(body.username, body.secret):
        _audit("anonymous", "regulator:login failed", "login")
        raise HTTPException(401, "Invalid credentials")
    _audit(body.username, "regulator:login ok", "login")
    return {"access_token": auth.create_token(body.username), "token_type": "bearer", "expires_in": auth.TOKEN_TTL_S}


@router.get("/reports")
def list_reports(status: Optional[Literal["open", "under_investigation", "confirmed", "dismissed"]] = None,
                 limit: int = Query(100, ge=1, le=500), _actor: str = Depends(regulator_call)):
    rows = db.all_("""SELECT r.id, r.scan_id, s.batch_number, r.report_type, r.description, r.image_reference, r.created_at, r.status
                        FROM reports r LEFT JOIN scan_events s ON s.id = r.scan_id
                       WHERE (%(st)s::text IS NULL OR r.status::text = %(st)s) ORDER BY r.created_at DESC LIMIT %(lim)s""",
                   {"st": status, "lim": limit})
    return {"reports": rows}


class StatusBody(BaseModel):
    status: Literal["under_investigation", "confirmed", "dismissed"]


@router.post("/reports/{report_id}/status")
def set_report_status(report_id: uuid.UUID, body: StatusBody, actor: str = Depends(regulator_call)):
    cur = db.one("SELECT status FROM reports WHERE id = %s", (report_id,))
    if not cur:
        raise HTTPException(404, "Report not found")
    if body.status not in REPORT_TRANSITIONS.get(cur["status"], set()):
        raise HTTPException(409, f"Invalid transition {cur['status']} -> {body.status}")
    upd = db.one("UPDATE reports SET status = %s WHERE id = %s AND status = %s RETURNING id", (body.status, report_id, cur["status"]))
    if not upd:                                                    # lost a race with another regulator call
        raise HTTPException(409, "Report status changed concurrently")
    _audit(actor, "regulator:report_status", f"report:{report_id} {cur['status']}->{body.status}")
    return {"id": str(report_id), "status": body.status}


@router.get("/disputes")
def list_disputes(status: Optional[Literal["open", "under_review", "resolved_upheld", "resolved_overturned"]] = None,
                  overdue: Optional[bool] = None, limit: int = Query(100, ge=1, le=500), _actor: str = Depends(regulator_call)):
    rows = db.all_("""SELECT * FROM (
                        SELECT id, batch_number, submitted_by, submitter_domain, evidence_reference, status, submitted_at, resolved_at,
                               resolution_notes, channel,
                               (status IN ('open','under_review') AND submitted_at < now() - make_interval(days => %(sla)s)) AS overdue
                          FROM disputes) d
                      WHERE (%(st)s::text IS NULL OR status::text = %(st)s) AND (%(od)s::boolean IS NULL OR overdue = %(od)s::boolean)
                      ORDER BY submitted_at DESC LIMIT %(lim)s""",
                   {"sla": sla_days(), "st": status, "od": overdue, "lim": limit})
    return {"sla_days": sla_days(), "disputes": rows}


class ResolveBody(BaseModel):
    resolution: Literal["upheld", "overturned"]
    notes: str = Field(min_length=1, max_length=2000)


@router.post("/disputes/{dispute_id}/resolve")
def resolve_dispute(dispute_id: uuid.UUID, body: ResolveBody, actor: str = Depends(regulator_call)):
    new = "resolved_overturned" if body.resolution == "overturned" else "resolved_upheld"
    with db.conn() as c, c.transaction():
        d = c.execute("""UPDATE disputes SET status = %s, resolved_at = now(), resolution_notes = %s
                          WHERE id = %s AND status IN ('open','under_review') RETURNING batch_number""",
                      (new, body.notes, dispute_id)).fetchone()
        if not d:
            exists = c.execute("SELECT status FROM disputes WHERE id = %s", (dispute_id,)).fetchone()
            raise HTTPException(404 if not exists else 409, "Dispute not found" if not exists else f"Dispute already {exists['status']}")
        c.execute("UPDATE batches SET dispute_status = %s WHERE batch_number = %s", (new, d["batch_number"]))
        if body.resolution == "overturned":
            c.execute("UPDATE nsq_alerts SET status = 'corrected' WHERE batch_number = %s AND status = 'active'", (d["batch_number"],))
        c.execute('INSERT INTO audit_log (id, actor, action, target, "timestamp") VALUES (gen_random_uuid(), %s, %s, %s, now())',
                  (actor[:200], "regulator:dispute_resolve", f"dispute:{dispute_id} {new}"))
    return {"id": str(dispute_id), "status": new, "batch_number": d["batch_number"]}
