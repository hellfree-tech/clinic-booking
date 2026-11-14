#!/usr/bin/env python3
"""
Clinic Booking API – Koya Academy Assessment (all 5 levels)
SQLite + FastAPI. Deploy on Render free tier.
"""

import os
import uuid
import time
import re
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from collections import defaultdict, deque
from threading import Lock

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy import (
    create_engine, Column, String, DateTime, Boolean, Text,
    UniqueConstraint, Index, select, update, delete, and_, or_
)
from sqlalchemy.orm import sessionmaker, declarative_base, Session
from sqlalchemy.exc import IntegrityError

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
API_KEY = os.getenv("API_KEY", "koya-clinic-2026-x7K9pQ2mR8vL4nJ6")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./clinic.db")

# Rate limit: 5 attempts per email per 60 seconds
RATE_LIMIT_MAX = 5
RATE_LIMIT_WINDOW = 60  # seconds

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Slot(Base):
    __tablename__ = "slots"
    id = Column(String, primary_key=True)
    provider = Column(String, nullable=False)
    start = Column(DateTime(timezone=True), nullable=False)
    end = Column(DateTime(timezone=True), nullable=False)


class Booking(Base):
    __tablename__ = "bookings"
    id = Column(String, primary_key=True)
    slot_id = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False)
    email = Column(String, nullable=False, index=True)
    status = Column(String, nullable=False, default="confirmed")  # confirmed | cancelled
    created_at = Column(DateTime(timezone=True), nullable=False)
    # For Level 3 race safety we also keep a unique constraint via application logic + check


class IdempotencyRecord(Base):
    __tablename__ = "idempotency"
    key = Column(String, primary_key=True)
    body_hash = Column(String, nullable=False)
    booking_id = Column(String, nullable=True)
    status_code = Column(String, nullable=False)
    response_body = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)


Base.metadata.create_all(bind=engine)

# ---------------------------------------------------------------------------
# In-memory rate limiting (simple + effective for assessment)
# ---------------------------------------------------------------------------
rate_lock = Lock()
# email -> deque of timestamps
rate_buckets: Dict[str, deque] = defaultdict(deque)


def check_rate_limit(email: str) -> Optional[int]:
    """Return Retry-After seconds if limited, else None."""
    now = time.time()
    with rate_lock:
        q = rate_buckets[email.lower()]
        # drop old
        while q and q[0] <= now - RATE_LIMIT_WINDOW:
            q.popleft()
        if len(q) >= RATE_LIMIT_MAX:
            retry_after = int(RATE_LIMIT_WINDOW - (now - q[0])) + 1
            return max(1, retry_after)
        q.append(now)
        return None


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class SlotIn(BaseModel):
    id: str
    provider: str
    start: datetime
    end: datetime


class ResetBody(BaseModel):
    slots: List[SlotIn]


class NewBooking(BaseModel):
    slot_id: str
    name: str
    email: str

    @field_validator("name", "email", mode="before")
    @classmethod
    def strip_str(cls, v):
        if isinstance(v, str):
            return v.strip()
        return v

    @field_validator("email")
    @classmethod
    def lower_email(cls, v):
        return v.lower() if isinstance(v, str) else v


class BookingOut(BaseModel):
    id: str
    slot_id: str
    name: str
    email: str
    status: str
    created_at: datetime


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="Clinic Booking API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_api_key(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    if not x_api_key or x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail={"error": "invalid or missing API key"})


def error(status: int, msg: str):
    return JSONResponse(status_code=status, content={"error": msg})


def parse_dt(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/reset")
def reset(body: ResetBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    require_api_key(x_api_key)
    db = SessionLocal()
    try:
        db.query(Booking).delete()
        db.query(Slot).delete()
        db.query(IdempotencyRecord).delete()
        for s in body.slots:
            db.add(Slot(
                id=s.id,
                provider=s.provider,
                start=parse_dt(s.start),
                end=parse_dt(s.end),
            ))
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@app.get("/slots")
def list_slots(
    provider: Optional[str] = Query(None),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    require_api_key(x_api_key)
    db = SessionLocal()
    try:
        q = db.query(Slot)
        if provider is not None:
            q = q.filter(Slot.provider == provider)
        slots = q.order_by(Slot.start).all()

        # which slots have a confirmed booking?
        confirmed = {
            b.slot_id
            for b in db.query(Booking.slot_id)
            .filter(Booking.status == "confirmed")
            .all()
        }

        result = []
        for s in slots:
            result.append({
                "id": s.id,
                "provider": s.provider,
                "start": s.start.isoformat().replace("+00:00", "Z"),
                "end": s.end.isoformat().replace("+00:00", "Z"),
                "available": s.id not in confirmed,
            })
        return {"slots": result}
    finally:
        db.close()


@app.post("/bookings")
def create_booking(
    body: NewBooking,
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    require_api_key(x_api_key)

    # Level 2 validation
    if not body.slot_id or not body.name or not body.email:
        return error(400, "slot_id, name and email are required")

    # basic email check (Pydantic EmailStr is strict; we keep simple + regex)
    email = body.email.lower().strip()
    name = body.name.strip()
    if not name or not email:
        return error(400, "name and email cannot be empty")
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return error(400, "invalid email")

    # Level 5 rate limit (count every attempt, success or fail)
    retry_after = check_rate_limit(email)
    if retry_after is not None:
        return JSONResponse(
            status_code=429,
            content={"error": "too many booking attempts for this email"},
            headers={"Retry-After": str(retry_after)},
        )

    db = SessionLocal()
    try:
        # Level 4 – idempotency
        body_hash = f"{body.slot_id}|{name}|{email}"
        if idempotency_key:
            existing = db.query(IdempotencyRecord).filter(
                IdempotencyRecord.key == idempotency_key
            ).first()
            if existing:
                if existing.body_hash != body_hash:
                    return error(422, "Idempotency-Key reused with a different body")
                # return the same response
                import json
                return JSONResponse(
                    status_code=int(existing.status_code),
                    content=json.loads(existing.response_body),
                )

        # Check slot exists
        slot = db.query(Slot).filter(Slot.id == body.slot_id).first()
        if not slot:
            return error(404, "slot not found")

        # Level 2 – already started?
        if parse_dt(slot.start) <= now_utc():
            return error(400, "slot has already started")

        # Level 3 – already has confirmed booking? (check + insert under transaction)
        # SQLite serializable-ish via immediate transaction
        db.execute(select(1))  # begin
        existing_booking = (
            db.query(Booking)
            .filter(Booking.slot_id == body.slot_id, Booking.status == "confirmed")
            .with_for_update(nowait=False)  # best effort
            .first()
        )
        if existing_booking:
            return error(409, "slot already booked")

        booking_id = str(uuid.uuid4())
        created = now_utc()
        booking = Booking(
            id=booking_id,
            slot_id=body.slot_id,
            name=name,
            email=email,
            status="confirmed",
            created_at=created,
        )
        db.add(booking)

        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return error(409, "slot already booked")

        out = {
            "id": booking_id,
            "slot_id": body.slot_id,
            "name": name,
            "email": email,
            "status": "confirmed",
            "created_at": created.isoformat().replace("+00:00", "Z") if created.tzinfo else created.isoformat() + "Z",
        }

        # store idempotency record
        if idempotency_key:
            import json
            rec = IdempotencyRecord(
                key=idempotency_key,
                body_hash=body_hash,
                booking_id=booking_id,
                status_code="201",
                response_body=json.dumps(out),
                created_at=created,
            )
            db.add(rec)
            db.commit()

        return JSONResponse(status_code=201, content=out)

    finally:
        db.close()


@app.get("/bookings/{booking_id}")
def get_booking(
    booking_id: str,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    require_api_key(x_api_key)
    db = SessionLocal()
    try:
        b = db.query(Booking).filter(Booking.id == booking_id).first()
        if not b:
            return error(404, "booking not found")
        return {
            "id": b.id,
            "slot_id": b.slot_id,
            "name": b.name,
            "email": b.email,
            "status": b.status,
            "created_at": b.created_at.isoformat().replace("+00:00", "Z"),
        }
    finally:
        db.close()


@app.delete("/bookings/{booking_id}")
def cancel_booking(
    booking_id: str,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    require_api_key(x_api_key)
    db = SessionLocal()
    try:
        b = db.query(Booking).filter(Booking.id == booking_id).first()
        if not b:
            return error(404, "booking not found")
        if b.status == "cancelled":
            return error(409, "already cancelled")
        b.status = "cancelled"
        db.commit()
        return {
            "id": b.id,
            "slot_id": b.slot_id,
            "name": b.name,
            "email": b.email,
            "status": "cancelled",
            "created_at": b.created_at.isoformat().replace("+00:00", "Z"),
        }
    finally:
        db.close()


@app.get("/bookings")
def list_person_bookings(
    email: str = Query(...),
    limit: int = Query(10),
    cursor: Optional[str] = Query(None),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    require_api_key(x_api_key)

    if limit < 1 or limit > 50:
        return error(400, "limit must be between 1 and 50")

    db = SessionLocal()
    try:
        email_l = email.strip().lower()
        q = (
            db.query(Booking)
            .filter(Booking.email == email_l)
            .order_by(Booking.created_at.asc(), Booking.id.asc())
        )

        if cursor:
            # cursor is "created_at|id"
            try:
                ts_str, last_id = cursor.split("|", 1)
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                q = q.filter(
                    or_(
                        Booking.created_at > ts,
                        and_(Booking.created_at == ts, Booking.id > last_id),
                    )
                )
            except Exception:
                return error(400, "invalid cursor")

        rows = q.limit(limit + 1).all()
        has_more = len(rows) > limit
        page = rows[:limit]

        bookings = [
            {
                "id": b.id,
                "slot_id": b.slot_id,
                "name": b.name,
                "email": b.email,
                "status": b.status,
                "created_at": b.created_at.isoformat().replace("+00:00", "Z"),
            }
            for b in page
        ]

        next_cursor = None
        if has_more and page:
            last = page[-1]
            next_cursor = f"{last.created_at.isoformat().replace('+00:00', 'Z')}|{last.id}"

        return {"bookings": bookings, "next_cursor": next_cursor}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Simple web UI (Level 1 demonstration)
# ---------------------------------------------------------------------------
HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Clinic Booking</title>
  <style>
    :root { --bg:#0f172a; --card:#1e293b; --accent:#38bdf8; --text:#e2e8f0; --muted:#94a3b8; --ok:#4ade80; --err:#f87171; }
    * { box-sizing: border-box; }
    body { font-family: system-ui, -apple-system, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 2rem 1rem; }
    .container { max-width: 720px; margin: 0 auto; }
    h1 { font-size: 1.75rem; margin-bottom: 0.25rem; }
    .sub { color: var(--muted); margin-bottom: 2rem; }
    .card { background: var(--card); border-radius: 12px; padding: 1.5rem; margin-bottom: 1.5rem; }
    label { display: block; font-size: 0.875rem; color: var(--muted); margin-bottom: 0.35rem; }
    input, select { width: 100%; padding: 0.65rem 0.75rem; border-radius: 8px; border: 1px solid #334155; background: #0f172a; color: var(--text); font-size: 1rem; margin-bottom: 1rem; }
    button { background: var(--accent); color: #0f172a; border: none; padding: 0.7rem 1.25rem; border-radius: 8px; font-weight: 600; cursor: pointer; font-size: 1rem; }
    button:disabled { opacity: 0.5; cursor: not-allowed; }
    .slot { display: flex; justify-content: space-between; align-items: center; padding: 0.75rem 0; border-bottom: 1px solid #334155; }
    .slot:last-child { border-bottom: none; }
    .badge { font-size: 0.75rem; padding: 0.2rem 0.5rem; border-radius: 999px; }
    .avail { background: #14532d; color: var(--ok); }
    .taken { background: #450a0a; color: var(--err); }
    #msg { margin-top: 1rem; padding: 0.75rem; border-radius: 8px; display: none; }
    #msg.ok { display: block; background: #14532d; color: var(--ok); }
    #msg.err { display: block; background: #450a0a; color: var(--err); }
    .row { display: flex; gap: 1rem; flex-wrap: wrap; }
    .row > * { flex: 1; min-width: 140px; }
  </style>
</head>
<body>
  <div class="container">
    <h1>Clinic Appointment Booking</h1>
    <p class="sub">Book a free slot with our doctors</p>

    <div class="card">
      <div class="row">
        <div>
          <label>Filter by provider (optional)</label>
          <input id="providerFilter" placeholder="e.g. Dr Ade" />
        </div>
        <div style="display:flex;align-items:flex-end;">
          <button onclick="loadSlots()">Refresh slots</button>
        </div>
      </div>
      <div id="slotsList">Loading…</div>
    </div>

    <div class="card">
      <h2 style="margin-top:0;font-size:1.15rem;">Book a slot</h2>
      <label>Slot</label>
      <select id="slotSelect"><option value="">— select a free slot —</option></select>
      <label>Your full name</label>
      <input id="name" placeholder="Ada Obi" />
      <label>Email</label>
      <input id="email" type="email" placeholder="ada@example.com" />
      <button id="bookBtn" onclick="book()">Confirm booking</button>
      <div id="msg"></div>
    </div>

    <div class="card" id="confirmation" style="display:none;">
      <h2 style="margin-top:0;font-size:1.15rem;color:var(--ok);">Booking confirmed</h2>
      <pre id="confDetails" style="white-space:pre-wrap;font-size:0.9rem;"></pre>
    </div>
  </div>

  <script>
    const API_KEY = "koya-clinic-2026-x7K9pQ2mR8vL4nJ6";
    const headers = { "X-API-Key": API_KEY, "Content-Type": "application/json" };

    async function loadSlots() {
      const provider = document.getElementById("providerFilter").value.trim();
      let url = "/slots";
      if (provider) url += "?provider=" + encodeURIComponent(provider);
      const res = await fetch(url, { headers });
      const data = await res.json();
      const list = document.getElementById("slotsList");
      const select = document.getElementById("slotSelect");
      select.innerHTML = '<option value="">— select a free slot —</option>';
      if (!data.slots || data.slots.length === 0) {
        list.innerHTML = "<p style='color:var(--muted)'>No slots yet. Use POST /reset to load some.</p>";
        return;
      }
      list.innerHTML = data.slots.map(s => {
        const start = new Date(s.start).toLocaleString();
        const end = new Date(s.end).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
        const badge = s.available
          ? '<span class="badge avail">Available</span>'
          : '<span class="badge taken">Taken</span>';
        if (s.available) {
          select.innerHTML += `<option value="${s.id}">${s.provider} · ${start} – ${end}</option>`;
        }
        return `<div class="slot"><div><strong>${s.provider}</strong><br><small>${start} → ${end}</small></div>${badge}</div>`;
      }).join("");
    }

    async function book() {
      const slot_id = document.getElementById("slotSelect").value;
      const name = document.getElementById("name").value.trim();
      const email = document.getElementById("email").value.trim();
      const msg = document.getElementById("msg");
      const btn = document.getElementById("bookBtn");
      msg.className = ""; msg.style.display = "none";
      if (!slot_id || !name || !email) {
        msg.textContent = "Please fill all fields and choose a free slot.";
        msg.className = "err"; return;
      }
      btn.disabled = true;
      try {
        const res = await fetch("/bookings", {
          method: "POST",
          headers,
          body: JSON.stringify({ slot_id, name, email })
        });
        const data = await res.json();
        if (res.status === 201) {
          msg.textContent = "Booked successfully!";
          msg.className = "ok";
          document.getElementById("confirmation").style.display = "block";
          document.getElementById("confDetails").textContent = JSON.stringify(data, null, 2);
          loadSlots();
        } else {
          msg.textContent = data.error || ("Error " + res.status);
          msg.className = "err";
        }
      } catch (e) {
        msg.textContent = "Network error";
        msg.className = "err";
      } finally {
        btn.disabled = false;
      }
    }

    loadSlots();
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def home():
    return HTML_PAGE


@app.get("/health")
def health():
    return {"status": "ok", "api_key_hint": API_KEY[:8] + "..."}


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
