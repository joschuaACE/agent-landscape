"""Operations team: always-on, read-only monitors (Outlook, Teams) that turn relevant messages into
mission proposals the human approves.

    Outlook Monitor ─┐  A2A                A2A
                     ├──▶ Communication ──────▶ Commander ──▶ proposal (pending) ──▶ human approves ──▶ mission
    Teams Monitor ───┘    Orchestrator          (region / topic check, pre-filled briefing)

Message sources only *read* (see :mod:`.sources`) – the team never sends, replies or changes anything.
"""
