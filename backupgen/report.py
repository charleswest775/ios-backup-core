"""Render a persona pack as a Markdown review document (``python -m backupgen report <persona>``).

Shows the conceit and time shift, the sourced timeline, the topics kept out, the contacts, every
conversation with both its real and on-device dates, and the calls, voicemails, notes, Safari
history, photos (with each photo's licence and credit), WhatsApp chats, calendar and voice
memos, so a reviewer can check facts and tone without reading JSON.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from backupgen.persona import Persona
from backupgen.persona.voicemail import UNKNOWN_CALLER


def _device_date(persona: Persona, real: datetime) -> str:
    return persona.device_time(real).astimezone(persona.tz).strftime("%Y-%m-%d")


def render(persona: Persona) -> str:
    out: list[str] = []
    w = out.append
    w(f"# {persona.name}: persona pack\n")
    if persona.summary:
        w(persona.summary + "\n")
    w("| | |\n|---|---|")
    w(f"| Real period | {persona.real_start} to {persona.real_end} |")
    w(f"| On the phone | shifted +{persona.shift_years} years: {persona.real_start.year + persona.shift_years}–{persona.real_end.year + persona.shift_years}, time zone {persona.tz.key} |")
    w(f"| Device | {persona.device_name} |")
    w(f"| Licence | {persona.licence} |")
    total = sum(len(c.messages) for c in persona.chats)
    extras = [(persona.calls, "calls"), (persona.voicemails, "voicemails"), (persona.notes, "notes"),
              (persona.visits, "Safari visits"), (persona.photos, "photos"), (persona.whatsapp, "WhatsApp chats"),
              (persona.events, "calendar events"), (persona.voice_memos, "voice memos")]
    more = "".join(f", {len(items)} {label}" for items, label in extras if items is not None)
    w(f"| Size | {len(persona.contacts) + 1} contacts, {len(persona.chats)} conversations, {total} messages{more} |\n")

    w("## Backups (snapshots)\n")
    w("| Snapshot | Backup date (on device) | Real-world equivalent | iPhone | iOS |\n|---|---|---|---|---|")
    for s in persona.snapshots.values():
        w(f"| `{s.id}` | {s.backup_date:%Y-%m-%d} | {persona.real_time(s.backup_date):%Y-%m-%d} | {s.product_name} | {s.ios} ({s.build}) |")
    w("")

    if persona.timeline:
        w("## Timeline\n")
        w("Every conversation is built on these documented events. Sources are listed per event.\n")
        w("| Real date | On device | Event | Sources |\n|---|---|---|---|")
        for event in persona.timeline:
            day = event["date"]
            try:
                shown = _device_date(persona, datetime.fromisoformat(day if len(day) == 10 else day + "-01" if len(day) == 7 else day + "-01-01"))
            except ValueError:
                shown = ""
            sources = " ".join(f"[{i + 1}]({url})" for i, url in enumerate(event.get("sources", [])))
            note = f" *({event['date_note']})*" if event.get("date_note") else ""
            w(f"| {day} | {shown} | **{event['title']}**: {event.get('detail', '')}{note} | {sources} |")
        w("")

    if persona.avoid:
        w("## Handled with care\n")
        w("Real events that are private, painful or disputed. The conversations leave them out or touch them only as stated here.\n")
        for topic in persona.avoid:
            w(f"- **{topic['topic']}** ({topic.get('date', '')}). {topic.get('guidance', '')} *Why: {topic.get('why_sensitive', '')}*")
        w("")

    w("## Contacts\n")
    w("| Name | Role | Phone | Email | Note |\n|---|---|---|---|---|")
    for c in [persona.owner, *persona.contacts.values()]:
        role = ", ".join(p for p in (c.job_title, c.organization) if p)
        nick = f" ({c.nickname})" if c.nickname else ""
        owner = " **(owner)**" if c is persona.owner else ""
        w(f"| {c.display_name}{nick}{owner} | {role} | {', '.join(n for _, n in c.phones)} | "
          f"{', '.join(e for _, e in c.emails)} | {c.note} |")
    w("")

    per_year = Counter(m.real_time.year for c in persona.chats for m in c.messages)
    w("## Messages per year\n")
    w("| " + " | ".join(str(y) for y in sorted(per_year)) + " |")
    w("|" + "---|" * len(per_year))
    w("| " + " | ".join(str(per_year[y]) for y in sorted(per_year)) + " |\n")

    w("## Conversations\n")
    for chat in persona.chats:
        people = ", ".join(persona.contacts[p].display_name for p in chat.participants)
        title = chat.name or people
        w(f"### {title}\n")
        w(f"*{chat.service}{', group with ' + people if chat.is_group else ''}. {len(chat.messages)} messages. File: `chats/{chat.id}.txt`*\n")
        day = None
        for m in chat.messages:
            if m.real_time.date() != day:
                day = m.real_time.date()
                w(f"\n**{day:%A %d %B %Y}** (on device: {_device_date(persona, m.real_time)})\n")
            contact = persona.owner if m.sender == "me" else persona.contacts[m.sender]
            who = contact.first or contact.display_name
            if m.reaction:
                w(f"- {m.real_time:%H:%M} *{who} reacted {m.reaction}*")
            elif m.photo:
                w(f"- {m.real_time:%H:%M} **{who}:** *[photo {m.photo}]*")
            else:
                w(f"- {m.real_time:%H:%M} **{who}:** {m.text}")
        w("")
    _calls(persona, w)
    _notes(persona, w)
    _visits(persona, w)
    _photos(persona, w)
    _whatsapp(persona, w)
    _calendar(persona, w)
    _voice_memos(persona, w)
    return "\n".join(out) + "\n"


def _who(persona: Persona, who: str) -> str:
    if who == UNKNOWN_CALLER:
        return "no number"
    return who if who.startswith("+") else persona.contacts[who].display_name


def _minutes(seconds: int) -> str:
    return f"{seconds // 60}m{seconds % 60:02d}s" if seconds else "missed"


def _calls(persona: Persona, w) -> None:
    if persona.calls is not None:
        w("## Calls\n")
        w("| When | Who | Direction | Length | Service |\n|---|---|---|---|---|")
        for c in persona.calls:
            w(f"| {c.real_time:%Y-%m-%d %H:%M} | {_who(persona, c.who)} | {'out' if c.outgoing else 'in'} | "
              f"{_minutes(c.duration)} | {c.service} |")
        w("")
    if persona.voicemails is not None:
        w("## Voicemails\n")
        w("| When | From | Length | Message |\n|---|---|---|---|")
        for v in persona.voicemails:
            kept = "".join(f" ({flag})" for flag in sorted(v.flags))
            w(f"| {v.real_time:%Y-%m-%d %H:%M} | {_who(persona, v.who)} | {_minutes(v.duration)}{kept} | {v.transcript} |")
        w("")


def _notes(persona: Persona, w) -> None:
    if persona.notes is None:
        return
    w("## Notes\n")
    for note in persona.notes:
        title, _, body = note.text.partition("\n")
        pinned = ", pinned" if note.pinned else ""
        deleted = f" Deleted {note.deleted:%Y-%m-%d %H:%M}." if note.deleted else ""
        where = "Old notes.sqlite (On My iPhone)" if note.store == "legacy" else note.folder
        shape = f", text can't be read (ZDATA {note.unreadable})" if note.unreadable else ""
        shape += ", empty snippet" if note.empty_snippet else ""
        w(f"### {title}\n")
        w(f"*{where}{pinned}{shape}. Created {note.created:%Y-%m-%d %H:%M}, modified {note.modified:%Y-%m-%d %H:%M}.{deleted} "
          f"File: `notes/{note.id}.txt`*\n")
        w("\n".join(f"> {line}" if line else ">" for line in body.strip("\n").split("\n")) + "\n")


def _visits(persona: Persona, w) -> None:
    if persona.visits is None:
        return
    w("## Safari\n")
    hosts = Counter(v.url.split("/")[2] for v in persona.visits)
    w("Most visited: " + ", ".join(f"{host} ({n})" for host, n in hosts.most_common(10)) + "\n")
    w("| When | Page | URL |\n|---|---|---|")
    for v in persona.visits:
        w(f"| {v.real_time:%Y-%m-%d %H:%M} | {v.title} | {v.url} |")
    w("")


def _photos(persona: Persona, w) -> None:
    if persona.photos is None:
        return
    w("## Photos\n")
    w("| Id | Taken | Place | Albums | Licence | Credit |\n|---|---|---|---|---|---|")
    for p in persona.photos:
        flags = (" ★" if p.favorite else "") + (" (deleted)" if p.trashed else "")
        source = f"[{p.licence or 'source'}]({p.source})" if p.source else (p.licence or "placeholder")
        w(f"| {p.id}{flags} | {p.real_time:%Y-%m-%d %H:%M} | {p.place} | {', '.join(p.albums)} | {source} | {p.credit} |")
    w("")


def _whatsapp(persona: Persona, w) -> None:
    if persona.whatsapp is None:
        return
    w("## WhatsApp\n")
    outsiders = [p for p in persona.whatsapp_people.values() if p.contact is None]
    if outsiders:
        w("Not in the address book: " + ", ".join(f"{p.push_name} ({p.number})" for p in outsiders) + "\n")

    def who(sender: str) -> str:
        if sender == "me":
            return persona.owner.first
        person = persona.whatsapp_people[sender]
        return person.contact.first if person.contact else person.push_name

    for chat in persona.whatsapp:
        people = ", ".join(who(p) for p in chat.participants)
        w(f"### {chat.name or people}\n")
        w(f"*WhatsApp {chat.type}{' with ' + people if chat.type != 'individual' else ''}. "
          f"{len(chat.messages)} lines. File: `whatsapp/{chat.id}.txt`*\n")
        day = None
        for m in chat.messages:
            if m.real_time.date() != day:
                day = m.real_time.date()
                w(f"\n**{day:%A %d %B %Y}** (on device: {_device_date(persona, m.real_time)})\n")
            star = " ★" if m.starred else ""
            if m.kind == "notice":
                if m.notice == "encrypted":
                    text = "end-to-end encryption notice"
                elif m.notice == "created":
                    text = f"{who(chat.creator)} created the group"
                else:
                    text = f"{who(m.member)} was added"
                w(f"- {m.real_time:%H:%M} *{text}*")
            elif m.kind == "reaction":
                w(f"- {m.real_time:%H:%M} *{who(m.sender)} reacted {m.reaction}*")
            elif m.kind == "text":
                reply = f" *(replying to {who(m.reply_to)})*" if m.reply_to else ""
                w(f"- {m.real_time:%H:%M} **{who(m.sender)}:**{reply} {m.text}{star}")
            else:
                call = f"{'video' if m.video else 'voice'} call, {_minutes(m.duration) if m.outcome == 'answered' else m.outcome}"
                detail = {"photo": f"photo {m.photo}", "voice": f"voice message, {m.duration}s",
                          "document": f"document {m.document}", "location": f"location {m.place}",
                          "live_location": "live location", "deleted": "deleted for everyone", "call": call}[m.kind]
                caption = f" {m.text}" if m.text else ""
                w(f"- {m.real_time:%H:%M} **{who(m.sender)}:** *[{detail}]*{caption}{star}")
        w("")


def _when(value) -> str:
    return f"{value:%Y-%m-%d %H:%M}" if isinstance(value, datetime) else f"{value:%Y-%m-%d}"


def _repeats(event) -> str:
    r = event.repeat
    if r is None:
        return ""
    every = {"daily": "day", "weekly": "week", "monthly": "month", "yearly": "year"}[r.frequency]
    text = f"every {every}" if r.interval == 1 else f"every {r.interval} {every}s"
    if r.by_day:
        text += " on " + ", ".join(f"{o}{d}" if o else d for o, d in r.by_day)
    if r.by_month:
        text += " in month " + ", ".join(str(m) for m in r.by_month)
    if r.count:
        text += f", {r.count} times"
    if r.until:
        text += f", until {_when(r.until)}"
    changes = [f"skips {_when(s)}" for s in event.skip] + [f"{_when(o)} moved to {_when(n)}" for o, n, _ in event.moved]
    return text + ("; " + "; ".join(changes) if changes else "")


def _calendar(persona: Persona, w) -> None:
    if persona.calendars is None:
        return
    w("## Calendar\n")
    titles = {c.id: c.title for c in persona.calendars}
    w("Calendars: " + ", ".join(f"{c.title} ({c.account}, {c.color})" for c in persona.calendars)
      + ". The phone also has its own Default and Birthdays calendars, without events.\n")

    def name(who: str) -> str:
        return persona.owner.first if who == "me" else persona.contacts[who].display_name

    w("| Starts | Ends | Calendar | Event | Details |\n|---|---|---|---|---|")
    for e in sorted(persona.events or [], key=lambda e: (_when(e.start), e.id)):
        zone = " floating" if e.floating else (f" {e.time_zone}" if e.time_zone else "")
        end_zone = f" {e.end_time_zone}" if e.end_time_zone else ""
        details = []
        if e.location:
            details.append(e.location)
        if e.organizer:
            people = ", ".join(f"{name(a)} ({e.statuses.get(a, 'needs_action')})" for a in e.attendees)
            details.append(f"organizer {name(e.organizer)}; invited {people}")
        if e.repeat:
            details.append("repeats " + _repeats(e))
        if e.alerts:
            details.append("alerts " + ", ".join(f"{m} min before" if m >= 0 else f"{-m} min after the start"
                                                 for m in e.alerts))
        if e.status:
            details.append(e.status)
        if e.availability:
            details.append(f"shows as {e.availability}")
        details.append(f"made {e.created:%Y-%m-%d}")
        w(f"| {_when(e.start)}{zone} | {_when(e.end)}{end_zone} | {titles[e.calendar]} | {e.title} | {'; '.join(details)} |")
    w("")


def _voice_memos(persona: Persona, w) -> None:
    if persona.voice_memos is None:
        return
    w("## Voice Memos\n")
    w("| Recorded | Length | Title | Folder | Flags | Transcript |\n|---|---|---|---|---|---|")
    for m in persona.voice_memos:
        flags = ", ".join(f for f, on in (("favourite", m.favorite), ("recently deleted", m.deleted),
                                           ("iCloud only", m.icloud)) if on)
        w(f"| {m.real_time:%Y-%m-%d %H:%M} | {_minutes(m.duration)} | {m.title} | {m.folder} | {flags} | {m.transcript} |")
    w("")
