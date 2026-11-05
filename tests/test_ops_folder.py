"""Folder source: Power Automate JSON files (OneDrive AgentInbox) → monitors → proposals. Read-only."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import httpx

from agent_landscape.ops.folder import FolderSource, find_inbox_dir
from test_ops import pending, running_with, wait_for

NOW = datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")  # Power Automate style (7 fractional digits)


def mail(**kw):
    return {"schema": "agentinbox.mail.v1", "source": "outlook", "id": "AAMkAD/x+y=", "conversationId": "conv-1",
            "internetMessageId": "<abc@contoso.com>", "receivedDateTime": iso(NOW), "from": "lena.vogel@contoso.com",
            "toRecipients": "me@contoso.com", "subject": "Request: vector DB comparison",
            "importance": "Normal", "hasAttachments": False, "bodyPreview": "Hi, could you…",
            "bodyText": "Hi,\n\ncould you prepare a comparison of Qdrant and pgvector by Friday?\n\nThanks, Lena", **kw}


def teams(kind="channel", **kw):
    return {"schema": "agentinbox.teams.v1", "source": "teams", "kind": kind, "messageId": "1730800000000",
            "replyToId": None, "messageType": "message", "teamId": "t1", "channelId": "19:abc@thread.tacv2",
            "chatId": None, "fromDisplayName": "Jonas Becker", "isFromApp": False, "createdDateTime": iso(NOW),
            "subject": None, "importance": "normal",
            "bodyText": "<p>can someone look into the LiteLLM 429s and propose a config fix?</p>",
            "mentions": [], "attachments": [], "webUrl": "https://teams.microsoft.com/l/message/x", **kw}


def build_inbox(tmp_path):
    root = tmp_path / "AgentInbox"
    for d in ("mail", "teams/channels", "teams/chats"):
        (root / d).mkdir(parents=True)
    return root


def write(p, data):
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


async def test_parse_and_read_only(tmp_path):
    root = build_inbox(tmp_path)
    write(root / "mail/20261105-090000_a.json", mail())
    write(root / "mail/20261105-090001_b.json", mail(receivedDateTime=iso(NOW - timedelta(days=3)),
                                                     internetMessageId="<old@x>"))
    (root / "mail/20261105-090002_c.json").write_text('{"half": ', encoding="utf-8")  # still syncing
    write(root / "teams/channels/1.json", teams())
    write(root / "teams/channels/2.json", teams(messageType="systemEventMessage", messageId="2"))
    write(root / "teams/chats/3.json", teams("chat", chatId="19:xyz@thread.v2", channelId=None, messageId="3",
                                            fromDisplayName="Joschua Brixner"))
    before = {p: p.stat().st_mtime for p in root.rglob("*.json")}

    out = FolderSource("outlook", root, backlog_hours=24)
    msgs = await out.fetch()
    assert [m.subject for m in msgs] == ["Request: vector DB comparison"]  # old one outside the backlog window
    m = msgs[0]
    assert m.id == "outlook:<abc@contoso.com>" and m.thread_id == "outlook-conv-1" and "Qdrant" in m.body
    assert out.errors and "20261105-090002_c.json" in out.errors[0] and "still syncing" in out.errors[0]
    (root / "mail/plain.json").write_text("\n\nAGENTTEST-MAIL-001 body only\n", encoding="utf-8")
    await out.fetch()
    assert any("plain.json" in e and "Html to text" in e for e in out.errors)
    (root / "mail/plain.json").unlink()
    assert await out.fetch() == []  # nothing new
    (root / "mail/20261105-090002_c.json").write_text(json.dumps(mail(internetMessageId="<c@x>")))
    assert len(await out.fetch()) == 1  # synced now → picked up

    tm = FolderSource("teams", root, ignore_senders=["joschua"])
    msgs = await tm.fetch()
    assert len(msgs) == 2  # system event skipped; own message kept but flagged (visible in the feed)
    assert msgs[0].location == "Teams channel" and msgs[0].body.startswith("can someone")
    assert msgs[1].meta["ignored"] and not msgs[0].meta.get("ignored")
    assert FolderSource("outlook", root, ignore_senders=["Joschua Brixner"])._ignored("joschua.brixner@valantic.com")
    before.pop(root / "mail/20261105-090002_c.json")  # rewritten by the test ("sync finished")
    assert {p: p.stat().st_mtime for p in root.rglob("*") if p in before} == before  # nothing touched
    assert len(list(root.rglob("*.json"))) == 6  # nothing moved or deleted


def test_find_inbox_dir(tmp_path, monkeypatch):
    assert find_inbox_dir(str(tmp_path / "missing")) is None
    root = build_inbox(tmp_path)
    assert find_inbox_dir(str(root)) == root
    monkeypatch.setenv("OneDriveCommercial", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert find_inbox_dir(None) == root


async def test_switching_to_folder_purges_mock_data(tmp_path):
    db = tmp_path / "ops.db"
    with running_with(db_path=str(db)) as base:  # mock source
        async with httpx.AsyncClient(timeout=30) as http:
            await http.post(f"{base}/api/ops/simulate", json={"channel": "outlook", "relevant": True})
            await wait_for(http, base, lambda o: pending(o))
            await asyncio.sleep(1.5)
    root = build_inbox(tmp_path)
    with running_with(db_path=str(db), ops_source="folder", ops_inbox_dir=str(root)) as base:
        async with httpx.AsyncClient(timeout=30) as http:
            ops = (await http.get(f"{base}/api/ops")).json()
            assert not ops["proposals"] and not ops["feed"] and not ops["mock"]
            assert all(m["seen"] == 0 for m in ops["monitors"])


async def test_manual_refresh_reads_the_folder_now(tmp_path):
    root = build_inbox(tmp_path)
    with running_with(ops_source="folder", ops_inbox_dir=str(root), ops_poll_seconds=3600) as base:
        async with httpx.AsyncClient(timeout=30) as http:
            await asyncio.sleep(0.5)  # first (startup) poll done – next one only in an hour
            write(root / "mail/new.json", mail())
            res = (await http.post(f"{base}/api/ops/refresh")).json()["channels"]
            assert res["outlook"]["new"] == 1 and res["outlook"]["files"] == 1 and res["teams"]["new"] == 0
            await wait_for(http, base, lambda o: pending(o))
            assert (await http.post(f"{base}/api/ops/refresh")).json()["channels"]["outlook"]["new"] == 0
    with running_with(ops_source="off") as base:
        assert httpx.post(f"{base}/api/ops/refresh").status_code == 409


async def test_waits_until_the_folder_is_synced(tmp_path):
    root = tmp_path / "AgentInbox"
    src = FolderSource("outlook", None, configured=str(root))
    assert await src.fetch() == [] and src.info()["waiting"] == str(root)
    build_inbox(tmp_path)
    write(root / "mail/x.json", mail())
    assert len(await src.fetch()) == 1 and src.info()["root"] == str(root)


async def test_folder_feeds_the_operations_team(tmp_path):
    root = build_inbox(tmp_path)
    write(root / "mail/existing.json", mail())
    with running_with(ops_source="folder", ops_inbox_dir=str(root)) as base:
        async with httpx.AsyncClient(timeout=30) as http:
            ops = await wait_for(http, base, lambda o: pending(o))
            assert ops["folder"] == str(root)
            assert {m["channel"]: m["source"]["files"] for m in ops["monitors"]}["outlook"] == 1
            p = pending(ops)[0]
            assert p["messages"][0]["sender"] == "lena.vogel@contoso.com" and "Qdrant" in p["intel"][0]["content"]
            # a new Teams message lands in the synced folder
            write(root / "teams/channels/new.json", teams())
            ops = await wait_for(http, base, lambda o: len(pending(o)) == 2)
            assert {q["channels"][0] for q in pending(ops)} == {"outlook", "teams"}
            assert (await http.post(f"{base}/api/ops/simulate", json={"channel": "teams"})).status_code == 400
            await asyncio.sleep(0)
    assert time.time()  # server stopped cleanly
