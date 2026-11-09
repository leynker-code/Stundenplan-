#!/usr/bin/env python3
"""Holt den Stundenplan aus WebUntis und schreibt ihn als .ics-Datei.

Zugangsdaten kommen ausschliesslich aus Umgebungsvariablen:
  UNTIS_SERVER   z. B. humboldt-schule.webuntis.com
  UNTIS_SCHOOL   Schulname laut WebUntis (steht nach dem Login in der URL: ?school=...)
  UNTIS_USER     Benutzername
  UNTIS_PASSWORD Passwort
  OUTPUT_FILE    Zieldatei (Standard: stundenplan.ics)

Nur Python-Standardbibliothek, keine Installation noetig.
"""
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")
WEEKS_AHEAD = 2  # aktuelle Woche + 2 Folgewochen


def env(name, default=None):
    value = os.environ.get(name, default)
    if not value:
        sys.exit(f"Fehler: Umgebungsvariable {name} fehlt.")
    return value


def rpc(url, method, params, session_id=None):
    body = json.dumps(
        {"id": "ical", "method": method, "params": params, "jsonrpc": "2.0"}
    ).encode()
    headers = {"Content-Type": "application/json"}
    if session_id:
        headers["Cookie"] = f"JSESSIONID={session_id}"
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
    except urllib.error.URLError as exc:
        sys.exit(f"Netzwerkfehler bei {method}: {exc}")
    if "error" in data:
        err = data["error"]
        sys.exit(f"WebUntis-Fehler bei {method}: {err.get('message')} (Code {err.get('code')})")
    return data["result"]


def name_of(items):
    """Liste von Fach-/Raum-/Lehrer-Objekten zu Text."""
    names = []
    for item in items or []:
        names.append(item.get("longname") or item.get("name") or "?")
    return ", ".join(names)


def short_of(items):
    return ", ".join(i.get("name") or i.get("longname") or "?" for i in items or [])


def to_dt(date_int, time_int):
    d = dt.datetime.strptime(str(date_int), "%Y%m%d")
    return d.replace(hour=time_int // 100, minute=time_int % 100, tzinfo=TZ)


def utc(d):
    return d.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def esc(text):
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def fold(line):
    """ICS-Zeilen auf max. 75 Bytes umbrechen."""
    raw = line.encode()
    if len(raw) <= 75:
        return line
    parts, current = [], b""
    for ch in line:
        b = ch.encode()
        limit = 75 if not parts else 74
        if len(current) + len(b) > limit:
            parts.append(current)
            current = b""
        current += b
    parts.append(current)
    return "\r\n ".join(p.decode() for p in parts)


def build_event(lesson, now):
    subject = name_of(lesson.get("su")) or "Unterricht"
    room = short_of(lesson.get("ro"))
    teacher = name_of(lesson.get("te"))
    code = lesson.get("code")

    if code == "cancelled":
        title = f"❌ Entfall: {subject}"
    elif code == "irregular":
        title = f"🔄 Geändert: {subject}"
    else:
        title = subject

    notes = []
    if code == "cancelled":
        notes.append("Diese Stunde entfällt.")
    elif code == "irregular":
        notes.append("Diese Stunde wurde geändert (Vertretung/Raum/Lehrkraft).")
    if teacher:
        notes.append(f"Lehrkraft: {teacher}")
    for key, label in (("substText", "Vertretungstext"), ("lstext", "Info"), ("info", "Hinweis")):
        if lesson.get(key):
            notes.append(f"{label}: {lesson[key]}")

    start = to_dt(lesson["date"], lesson["startTime"])
    end = to_dt(lesson["date"], lesson["endTime"])
    uid = f"{lesson['id']}-{lesson['date']}-{lesson['startTime']}@untis-ical"

    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{utc(now)}",
        f"DTSTART:{utc(start)}",
        f"DTEND:{utc(end)}",
        f"SUMMARY:{esc(title)}",
    ]
    if room:
        lines.append(f"LOCATION:{esc(room)}")
    if notes:
        lines.append(f"DESCRIPTION:{esc(chr(10).join(notes))}")
    lines.append("END:VEVENT")
    return "\r\n".join(fold(l) for l in lines)


def main():
    server = env("UNTIS_SERVER")
    school = env("UNTIS_SCHOOL")
    user = env("UNTIS_USER")
    password = env("UNTIS_PASSWORD")
    out_file = os.environ.get("OUTPUT_FILE", "stundenplan.ics")

    url = f"https://{server}/WebUntis/jsonrpc.do?school={urllib.parse.quote(school)}"

    auth = rpc(url, "authenticate", {"user": user, "password": password, "client": "untis-ical"})
    session_id = auth["sessionId"]
    person_id, person_type = auth["personId"], auth["personType"]

    today = dt.datetime.now(TZ).date()
    monday = today - dt.timedelta(days=today.weekday())
    sunday_last = monday + dt.timedelta(days=7 * (WEEKS_AHEAD + 1) - 1)

    fields = ["id", "name", "longname"]
    lessons = rpc(
        url,
        "getTimetable",
        {
            "options": {
                "element": {"id": person_id, "type": person_type},
                "startDate": int(monday.strftime("%Y%m%d")),
                "endDate": int(sunday_last.strftime("%Y%m%d")),
                "showInfo": True,
                "showSubstText": True,
                "showLsText": True,
                "showStudentgroup": True,
                "klasseFields": fields,
                "roomFields": fields,
                "subjectFields": fields,
                "teacherFields": fields,
            }
        },
        session_id,
    )

    try:
        rpc(url, "logout", {}, session_id)
    except SystemExit:
        pass  # Abmelden ist nicht kritisch

    now = dt.datetime.now(dt.timezone.utc)
    lessons.sort(key=lambda l: (l["date"], l["startTime"]))
    events = [build_event(l, now) for l in lessons]

    calendar = "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//untis-ical//DE",
            "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH",
            "X-WR-CALNAME:Stundenplan",
            "X-WR-TIMEZONE:Europe/Berlin",
            "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
            "X-PUBLISHED-TTL:PT1H",
            *events,
            "END:VCALENDAR",
            "",
        ]
    )
    os.makedirs(os.path.dirname(out_file) or ".", exist_ok=True)
    with open(out_file, "w", encoding="utf-8", newline="") as fh:
        fh.write(calendar)

    cancelled = sum(1 for l in lessons if l.get("code") == "cancelled")
    changed = sum(1 for l in lessons if l.get("code") == "irregular")
    print(f"{len(lessons)} Stunden gespeichert ({cancelled} Entfall, {changed} geändert) -> {out_file}")


if __name__ == "__main__":
    main()
