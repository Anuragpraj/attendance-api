import json, os, re
from calendar import monthrange
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()
db = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, serverSelectionTimeoutMS=5000)[os.getenv("MONGO_DB", "attendance_db")]
IST, UTC = timezone(timedelta(hours=5, minutes=30)), timezone.utc
PRES = ["PRESENT", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
Ms = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]
HM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
Month = Annotated[str, Query(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")]
Page = Annotated[int, Query(ge=1)]
Size = Annotated[int, Query(ge=1, le=100)]
Limit = Annotated[int, Query(ge=1, le=50)]


@asynccontextmanager
async def lifespan(_):
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", 1), ("emp_code", 1)])
    db.employees.create_index("joined_on")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


def ms(v):
    if isinstance(v, datetime):
        return (v.replace(tzinfo=v.tzinfo or UTC) - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(milliseconds=1)
    return v


def to_dt(m):
    return datetime.fromtimestamp(m // 1000, UTC)


def rnd(x, n=2):
    return None if x is None else float(Decimal(repr(round(x, 9))).quantize(Decimal(1).scaleb(-n), ROUND_HALF_UP))


def pdate(s):
    try:
        d = date.fromisoformat(s) if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", s) else None
    except ValueError:
        d = None
    if d is None or not 1000 <= d.year <= 9000:
        raise HTTPException(422, "invalid date, expected YYYY-MM-DD")
    return d


def hm(s):
    return time(int(s[:2]), int(s[3:]))


def shift_dt(d, s):
    return datetime.combine(d, hm(s), IST)


def att_date(t, e):
    local = t.astimezone(IST)
    if e["shift_end"] <= e["shift_start"] and local.time() < hm(e["shift_end"]):
        return local.date() - timedelta(days=1)
    return local.date()


def derive(e, d, pin, pout):
    out = {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    if pin is None:
        return out
    sec = (pin - shift_dt(d, e["shift_start"])).total_seconds()
    out["late_minutes"] = int(sec // 60) if sec > 600 else 0
    if pout:
        end = shift_dt(d, e["shift_end"]) + timedelta(days=e["shift_end"] <= e["shift_start"])
        ot = int((pout - end).total_seconds() // 60)
        wh = rnd((pout - pin).total_seconds() / 3600)
        out |= {"work_hours": wh, "overtime_minutes": ot if ot >= 30 else 0, "half_day": wh < 4.5}
    return out


def rec(d):
    return {
        "emp_code": d["emp_code"], "date": d["date"], "status": d["status"],
        "punch_in": ms(d.get("punch_in")), "punch_out": ms(d.get("punch_out")), "work_hours": d.get("work_hours"),
        "late_minutes": d.get("late_minutes") or 0, "overtime_minutes": d.get("overtime_minutes") or 0,
        "half_day": bool(d.get("half_day")),
        "history": [{"at": ms(h["at"]), "by": h["by"], "reason": h["reason"],
                     "changes": {k: {"from": ms(v.get("from")), "to": ms(v.get("to"))} for k, v in h["changes"].items()}}
                    for h in d.get("history") or []],
    }


def emp_out(e):
    return {**{k: e.get(k) for k in ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on")}, "created_at": ms(e.get("created_at"))}


def emp_or_404(c):
    e = db.employees.find_one({"emp_code": c})
    if not e:
        raise HTTPException(404, "employee not found")
    return e


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HM)
    shift_end: str = Field("18:30", pattern=HM)
    joined_on: str

    @model_validator(mode="after")
    def check(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        pdate(self.joined_on)
        return self


class Punch(BaseModel):
    emp_code: str
    punched_at: Optional[Ms] = None


class PunchIn(Punch):
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class RegIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[Ms] = None
    punch_out: Optional[Ms] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


@app.get("/health")
def health():
    try:
        db.command("ping")
    except Exception:
        raise HTTPException(503, "mongodb unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(b: EmployeeIn):
    doc = {**b.model_dump(), "created_at": datetime.now(UTC)}
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return emp_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: Size = 20):
    q = {"department": department} if department else {}
    items = db.employees.find(q).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [emp_out(e) for e in items], "total": db.employees.count_documents(q), "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(b: PunchIn):
    e = emp_or_404(b.emp_code)
    t = to_dt(b.punched_at) if b.punched_at else datetime.now(UTC).replace(microsecond=0)
    d = att_date(t, e)
    doc = {"emp_code": b.emp_code, "date": d.isoformat(), "status": b.status, "punch_in": t, "punch_out": None, **derive(e, d, t, None), "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return rec(doc)


@app.post("/attendance/punch-out")
def punch_out(b: Punch):
    e = emp_or_404(b.emp_code)
    t = to_dt(b.punched_at) if b.punched_at else datetime.now(UTC).replace(microsecond=0)
    r = db.attendance_logs.find_one({"emp_code": b.emp_code, "punch_in": {"$lte": t}}, sort=[("punch_in", -1)])
    if not r:
        raise HTTPException(404, "no punch-in found")
    if not r["punch_in"] < t <= r["punch_in"] + timedelta(hours=24):
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")
    u = db.attendance_logs.find_one_and_update(
        {"emp_code": b.emp_code, "date": r["date"], "punch_out": None},
        {"$set": {"punch_out": t, **derive(e, date.fromisoformat(r["date"]), r["punch_in"], t)}},
        return_document=ReturnDocument.AFTER)
    if not u:
        raise HTTPException(409, "already punched out")
    return rec(u)


def att_find(emp_code, date_from, date_to, status, page, size):
    r = {k: pdate(v).isoformat() for k, v in (("$gte", date_from), ("$lte", date_to)) if v}
    if len(r) == 2 and r["$gte"] > r["$lte"]:
        raise HTTPException(422, "date_from is after date_to")
    f = {k: v for k, v in (("emp_code", emp_code), ("status", status), ("date", r)) if v}
    return {"filter": f, "sort": [("date", -1), ("emp_code", 1)], "skip": (page - 1) * size, "limit": size}


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                    status: Optional[Status] = None, page: Page = 1, page_size: Size = 20):
    kw = att_find(emp_code, date_from, date_to, status, page, page_size)
    L = db.attendance_logs
    total = L.count_documents(kw["filter"]) if kw["filter"] else L.estimated_document_count()
    return {"items": [rec(d) for d in L.find(**kw)], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, b: RegIn):
    d = pdate(date)
    e = emp_or_404(emp_code)
    r = db.attendance_logs.find_one({"emp_code": emp_code, "date": d.isoformat()})
    if not r:
        raise HTTPException(404, "no record for that date")
    st = b.status or r["status"]
    pin = to_dt(b.punch_in) if b.punch_in else r.get("punch_in")
    pout = to_dt(b.punch_out) if b.punch_out else r.get("punch_out")
    if st in ("ABSENT", "LEAVE"):
        if b.punch_in or b.punch_out:
            raise HTTPException(422, "ABSENT/LEAVE cannot carry punch times")
        pin = pout = None
    elif pin is None:
        raise HTTPException(422, "presence status requires punch_in")
    if b.punch_in and att_date(pin, e) != d:
        raise HTTPException(422, "punch_in must stay on the record's attendance date")
    if pout and (b.punch_in or b.punch_out) and not pin < pout <= pin + timedelta(hours=24):
        raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    new = {"status": st, "punch_in": pin, "punch_out": pout, **derive(e, d, pin, pout)}
    dflt = {"late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    ch = {k: {"from": r.get(k, dflt.get(k)), "to": v} for k, v in new.items() if r.get(k, dflt.get(k)) != v}
    if not {"status", "punch_in", "punch_out"} & ch.keys():
        raise HTTPException(422, "request changes nothing")
    entry = {"at": datetime.now(UTC), "by": b.regularized_by, "reason": b.reason, "changes": ch}
    n = len(r.get("history") or [])
    u = db.attendance_logs.find_one_and_update(
        {"emp_code": emp_code, "date": r["date"], "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, n]}},
        {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
    if not u:
        raise HTTPException(409, "record was modified concurrently, retry")
    return rec(u)


def span(m):
    y, mo = int(m[:4]), int(m[5:])
    if y < 1:
        raise HTTPException(422, "invalid month")
    days = [date(y, mo, i) for i in range(1, monthrange(y, mo)[1] + 1)]
    return days[0].isoformat(), days[-1].isoformat(), [x.isoformat() for x in days if x.weekday() < 5]


def acc(p, wd):
    f = lambda x: "$" + p + x
    pres = {"$in": [f("status"), PRES]}
    cnt = lambda c: {"$sum": {"$cond": [c, 1, 0]}}
    return {
        "present_days": {"$sum": {"$cond": [{"$and": [pres, {"$in": [f("date"), wd]}]}, {"$cond": [f("half_day"), 0.5, 1]}, 0]}},
        "late_count": cnt({"$gt": [f("late_minutes"), 0]}),
        "total_late_minutes": {"$sum": f("late_minutes")},
        "total_overtime_minutes": {"$sum": f("overtime_minutes")},
        "leave_count": cnt({"$eq": [f("status"), "LEAVE"]}),
        "on_duty_count": cnt({"$eq": [f("status"), "ON_DUTY"]}),
        "avg_work_hours": {"$avg": {"$cond": [pres, f("work_hours"), None]}},
    }


def monthly_pipe(c, m):
    s, e, wd = span(m)
    return "attendance_logs", [{"$match": {"emp_code": c, "date": {"$gte": s, "$lte": e}}}, {"$group": {"_id": None, **acc("", wd)}}]


def dept_pipe(m, dep):
    s, e, wd = span(m)
    return "employees", [
        {"$match": {"joined_on": {"$lte": e}, **({"department": dep} if dep else {})}},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                     "pipeline": [{"$match": {"date": {"$gte": s, "$lte": e}}}], "as": "l"}},
        {"$unwind": {"path": "$l", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$department", "emps": {"$addToSet": "$emp_code"}, **acc("l.", wd)}},
        {"$set": {"headcount": {"$size": "$emps"}}}, {"$unset": "emps"}, {"$sort": {"_id": 1}}]


def late_pipe(m, limit, dep):
    s, e, _ = span(m)
    return "attendance_logs", [
        {"$match": {"date": {"$gte": s, "$lte": e}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},
        *([{"$match": {"e.department": dep}}] if dep else []),
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$e.name", "department": "$e.department",
                      "total_late_minutes": 1, "late_count": 1}}]


def trend_pipe(dep, frm, to):
    f, t = pdate(frm), pdate(to)
    if t < f or (t - f).days >= 92:
        raise HTTPException(422, "invalid range: to < from or more than 92 days")
    emps = list(db.employees.find({"department": dep}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not emps:
        raise HTTPException(404, "unknown department")
    lo = datetime.combine(f, time(), UTC)
    g = acc("", [(f + timedelta(i)).isoformat() for i in range((t - f).days + 1)])
    return "attendance_logs", [
        {"$match": {"emp_code": {"$in": [x["emp_code"] for x in emps]}, "date": {"$gte": f.isoformat(), "$lte": t.isoformat()}}},
        {"$group": {"_id": "$date", "present_count": g["present_days"], "late_count": g["late_count"]}},
        {"$set": {"dt": {"$dateFromString": {"dateString": "$_id"}}}},
        {"$densify": {"field": "dt", "range": {"step": 1, "unit": "day", "bounds": [lo, datetime.combine(t, time(), UTC) + timedelta(days=1)]}}},
        {"$set": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$dt"}}, "is_working_day": {"$lte": [{"$isoDayOfWeek": "$dt"}, 5]},
                  "present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]}}},
        {"$set": {"headcount": {"$size": {"$filter": {"input": [x["joined_on"] for x in emps], "as": "j", "cond": {"$lte": ["$$j", "$date"]}}}}}},
        {"$set": {"rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$divide": ["$present_count", "$headcount"]}, None]}}},
        {"$setWindowFields": {"sortBy": {"dt": 1}, "output": {"ma": {"$avg": "$rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"dt": 1}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
                      "attendance_rate": "$rate", "moving_avg_7d": "$ma"}}]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Month):
    e = emp_or_404(emp_code)
    r = next(db.attendance_logs.aggregate(monthly_pipe(emp_code, month)[1]), {})
    wd = sum(d >= e["joined_on"] for d in span(month)[2])
    pd = rnd(r.get("present_days", 0))
    return {"emp_code": emp_code, "month": month, "working_days": wd, "present_days": pd, "leave_days": r.get("leave_count", 0),
            "late_count": r.get("late_count", 0), "total_late_minutes": r.get("total_late_minutes", 0),
            "total_overtime_minutes": r.get("total_overtime_minutes", 0), "attendance_pct": rnd(pd / wd * 100) if wd else None}


@app.get("/analytics/departments/summary")
def department_summary(month: Month, department: Optional[str] = None):
    items = [{"department": r["_id"], "headcount": r["headcount"], "present_days": rnd(r["present_days"]),
              "avg_work_hours": rnd(r["avg_work_hours"]), "late_count": r["late_count"], "total_late_minutes": r["total_late_minutes"],
              "leave_count": r["leave_count"], "on_duty_count": r["on_duty_count"]}
             for r in db.employees.aggregate(dept_pipe(month, department)[1])]
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: Month, limit: Limit = 10, department: Optional[str] = None):
    return {"month": month, "items": list(db.attendance_logs.aggregate(late_pipe(month, limit, department)[1]))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: Annotated[str, Query(alias="from")], to: str):
    rows = db.attendance_logs.aggregate(trend_pipe(department, from_, to)[1])
    return {"department": department, "items": [{**r, "attendance_rate": rnd(r["attendance_rate"], 4), "moving_avg_7d": rnd(r["moving_avg_7d"], 4)} for r in rows]}


@app.get("/admin/explain/{endpoint}")
def explain(endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
            emp_code: Optional[str] = None, month: Annotated[Optional[str], Query(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")] = None,
            department: Optional[str] = None, limit: Limit = 10, date_from: Optional[str] = None, date_to: Optional[str] = None,
            status: Optional[Status] = None, from_: Annotated[Optional[str], Query(alias="from")] = None, to: Optional[str] = None,
            page: Page = 1, page_size: Size = 20):
    need = {"attendance_list": [1], "employee_monthly": [emp_code, month], "department_summary": [month],
            "late_leaderboard": [month], "department_trend": [department, from_, to]}[endpoint]
    if not all(need):
        raise HTTPException(422, "missing required query parameters")
    if endpoint == "attendance_list":
        kw = att_find(emp_code, date_from, date_to, status, page, page_size)
        coll, cmd = "attendance_logs", {"find": "attendance_logs", **kw, "sort": dict(kw["sort"])}
    else:
        coll, pipe = {"employee_monthly": lambda: monthly_pipe(emp_code, month), "department_summary": lambda: dept_pipe(month, department),
                      "late_leaderboard": lambda: late_pipe(month, limit, department), "department_trend": lambda: trend_pipe(department, from_, to)}[endpoint]()
        cmd = {"aggregate": coll, "pipeline": pipe, "cursor": {}}
    res = db.command({"explain": cmd, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": coll, "explain": json.loads(json_util.dumps(res))}
