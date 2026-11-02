"""Render a persona pack as a Markdown review document (``python -m backupgen report <persona>``).

Shows the conceit and time shift, the sourced timeline, the topics kept out, the contacts, every
conversation with both its real and on-device dates, and the calls, voicemails, notes, Safari
history and photos (with each photo's licence and credit), so a reviewer can check facts and
tone without reading JSON.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from backupgen.persona import Persona


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
              (persona.visits, "Safari visits"), (persona.photos, "photos")]
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
    return "\n".join(out) + "\n"


def _who(persona: Persona, who: str) -> str:
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
            w(f"| {v.real_time:%Y-%m-%d %H:%M} | {_who(persona, v.who)} | {_minutes(v.duration)} | {v.transcript} |")
        w("")


def _notes(persona: Persona, w) -> None:
    if persona.notes is None:
        return
    w("## Notes\n")
    for note in persona.notes:
        title, _, body = note.text.partition("\n")
        pinned = ", pinned" if note.pinned else ""
        w(f"### {title}\n")
        w(f"*{note.folder}{pinned}. Created {note.created:%Y-%m-%d %H:%M}, modified {note.modified:%Y-%m-%d %H:%M}. "
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
