"""Render a persona pack as a Markdown review document (``python -m backupgen report <persona>``).

Shows the conceit and time shift, the sourced timeline, the contacts and every
conversation with both its real and on-device dates, so a reviewer can check
facts and tone without reading JSON.
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
    w(f"| Size | {len(persona.contacts) + 1} contacts, {len(persona.chats)} conversations, {total} messages |\n")

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
            else:
                w(f"- {m.real_time:%H:%M} **{who}:** {m.text}")
        w("")
    return "\n".join(out) + "\n"
