"""`polar mail inbox --since <epoch>`: new mail for the owner as JSON lines in the `polar inbox` shape (fake Gmail)."""
import json
import re

from polar import cli, gmail
from tests.test_gmail import FakeGmail, b64, msg

ME = "me@corp.example"
T0 = 1700000000  # `since`; the default message time of msg() is exactly T0


def mail(mid, frm="Ana Pérez <ana@client.example>", to=ME, ms=(T0 + 60) * 1000, headers=(), thread="T1", **kw):
    m = msg(mid, frm, to, ms=ms, **kw)
    m["threadId"] = thread
    m["payload"]["headers"] += [{"name": k, "value": v} for k, v in headers]
    return m


def rows(*messages, since=T0):
    svc = FakeGmail(list(messages))
    return svc, gmail.inbox(svc, since)[0]


def test_new_message_has_the_inbox_shape():
    svc, out = rows(mail("m1", subject="Re: REGLAS ACME", body="Va la regla nueva."))
    assert out == [{
        "source": "mail", "ts": "1700000060.000", "channel": "mail", "thread_ts": "T1", "message_id": "m1",
        "sender": "Ana Pérez", "sender_id": "ana@client.example", "subject": "Re: REGLAS ACME",
        "text": "Re: REGLAS ACME — Va la regla nueva.", "permalink": "https://mail.google.com/mail/u/0/#all/T1",
        "is_dm": True, "to_owner": True, "auth_ok": False, "internal": False}]
    listed = svc.of("messages.list")[0]
    assert listed["q"] == "after:1700000000 -from:me -in:chats" and listed["userId"] == "me"


def test_old_and_own_messages_are_left_out():
    _, out = rows(mail("old", ms=T0 * 1000), mail("mine", frm="Yo <ME@corp.example>"), mail("new"))
    assert [r["message_id"] for r in out] == ["new"]


def test_rows_are_sorted_oldest_first():
    _, out = rows(mail("b", ms=(T0 + 90) * 1000), mail("a", ms=(T0 + 30) * 1000))
    assert [r["message_id"] for r in out] == ["a", "b"]


def test_automated_senders_are_left_out():
    _, out = rows(
        mail("n1", frm="GitHub <notifications@github.com>"),
        mail("n2", frm="no-reply@svc.example"),
        mail("n3", frm="Banco <noreply+abc@bank.example>"),
        mail("n4", headers=[("List-Id", "<news.client.example>")]),
        mail("n5", headers=[("Precedence", "bulk")]),
        mail("n6", headers=[("Auto-Submitted", "auto-replied")]),
        mail("n7", frm="Mail Delivery <mailer-daemon@googlemail.com>"),
        mail("ok", headers=[("Auto-Submitted", "no")]),
        mail("ok2", frm="Remy Nolan <rnolan@client.example>"),  # «no» inside a name is not no-reply
    )
    assert sorted(r["message_id"] for r in out) == ["ok", "ok2"]


def test_calendar_invitations_and_reactions_are_left_out():
    invite = mail("cal")
    invite["payload"]["parts"].append({"mimeType": "text/calendar", "body": {"data": b64("BEGIN:VCALENDAR")}})
    ics = mail("ics")
    ics["payload"]["parts"].append({"mimeType": "application/octet-stream", "filename": "invite.ics", "body": {}})
    react = mail("react")
    react["payload"]["parts"].append({"mimeType": "text/vnd.google.email-reaction+json", "body": {"data": b64("{}")}})
    _, out = rows(invite, ics, react, mail("ok"))
    assert [r["message_id"] for r in out] == ["ok"]


def test_quoted_reply_and_signature_are_removed():
    body = ("Hola Alex,\nya quedó la regla.\n\n-- \nAna Pérez\nDirectora\n\n"
            "El lun, 20 nov 2026 a las 10:00, Alex (<me@corp.example>) escribió:\n> ¿Cómo va?\n")
    assert gmail.new_text(body) == "Hola Alex,\nya quedó la regla."
    assert gmail.new_text("Listo.\n\nOn Mon, Nov 20, 2026 at 10:00 AM Alex <me@corp.example>\nwrote:\n> hi") == "Listo."
    assert gmail.new_text("Va.\n> cita\n> cita 2\nabajo") == "Va.\nabajo"
    assert gmail.new_text("Ok\n\n-----Original Message-----\nFrom: x") == "Ok"
    assert gmail.new_text("Ok\n\nDe: Alex <me@corp.example>\nEnviado: lunes\nPara: Ana\n\nviejo") == "Ok"
    assert gmail.new_text("El pago salió hoy.\nDe: la cuenta 2 va el resto.") == "El pago salió hoy.\nDe: la cuenta 2 va el resto."


def test_answer_below_the_quote_and_plain_words_that_look_like_a_quote_head_stay():
    assert gmail.new_text("> ¿Cómo va la regla?\n\nYa quedó, mañana la subo.") == "Ya quedó, mañana la subo."
    body = "Hi,\nOn the route issue:\nJuan wrote:\nthe route is closed."
    assert gmail.new_text(body) == body


def test_only_quoted_text_keeps_the_body():
    body = "---------- Forwarded message ---------\nFrom: x\n\nel contrato"
    assert gmail.new_text(body) == body


def test_text_is_capped_at_600_characters_of_new_text():
    _, out = rows(mail("m1", subject="S", body="x" * 900))
    assert out[0]["text"] == "S — " + "x" * 600


def ar(value):
    return ("Authentication-Results", "mx.google.com; " + value)


WORKSPACE = ar("dkim=pass header.i=@corp-example.20260707.gappssmtp.com header.s=x; spf=neutral smtp.mailfrom=sam@corp.example; "
               "dmarc=fail (p=NONE sp=NONE dis=NONE) header.from=corp.example")


def test_to_owner_flag():
    _, out = rows(
        mail("cc", frm="Sam <sam@corp.example>", to="otra@corp.example", headers=[("Cc", ME)]),
        mail("to", frm="Sam <sam@CORP.example>", to="Otra <otra@corp.example>, Yo <ME@corp.example>", ms=(T0 + 61) * 1000))
    assert [(r["message_id"], r["to_owner"]) for r in out] == [("cc", False), ("to", True)]


def flags(frm, *headers):
    _, out = rows(mail("m", frm=frm, headers=list(headers)))
    return out[0]["auth_ok"], out[0]["internal"]


def test_sender_is_proven_only_by_googles_own_check_of_the_from_domain():
    own = "Sam <sam@corp.example>"
    assert flags(own) == (False, False)                                    # no header: not proven
    assert flags(own, WORKSPACE) == (True, True)                           # the domain's Google Workspace signed it
    assert flags(own, ar("dkim=pass header.i=@corp.example; spf=pass; dmarc=pass (p=NONE) header.from=corp.example")) == (True, True)
    assert flags(own, ar("dkim=pass header.i=@corp.example; dmarc=fail header.from=corp.example")) == (True, True)
    # a forged «From»: the attacker's own DKIM passes, and DMARC fails with policy none (the mail still arrives)
    assert flags(own, ar("dkim=pass header.i=@evil.example; spf=pass smtp.mailfrom=evil.example; "
                         "dmarc=fail (p=NONE) header.from=corp.example")) == (False, False)
    assert flags(own, ar("dkim=pass header.i=@corp-example.20260707.gappssmtp.com.evil.example; dmarc=fail header.from=corp.example")) == (False, False)
    assert flags(own, ar("dkim=pass header.i=@notcorp-example.20260707.gappssmtp.com; dmarc=fail header.from=corp.example")) == (False, False)
    assert flags(own, ar("spf=softfail smtp.mailfrom=x; dmarc=fail header.from=corp.example")) == (False, False)
    # text the sender controls inside Google's header is not a result: the envelope sender may be «dmarc=pass@…»
    assert flags(own, ar("spf=pass (google.com: domain of dmarc=pass@evil.example designates 1.2.3.4 as permitted sender) "
                         "smtp.mailfrom=dmarc=pass@evil.example; dmarc=fail (p=NONE sp=NONE dis=NONE) header.from=corp.example")) == (False, False)
    assert flags(own, ar("spf=neutral (google.com: 1.2.3.4 is neither permitted nor denied by best guess record for domain of "
                         "dkim=pass+header.i=@corp.example) smtp.mailfrom=dkim=pass+header.i=@corp.example; "
                         "dmarc=fail (p=NONE) header.from=corp.example")) == (False, False)
    assert flags(own, ar("spf=pass smtp.mailfrom=dkim=pass+header.i=@corp-example.20260707.gappssmtp.com; dmarc=fail header.from=corp.example")) == (False, False)
    # anything this strict reader does not know is «not proven»: a quote, a bracket left open, a second DMARC result
    assert flags(own, ar('spf=pass (domain of "a) ; dmarc=pass header.from=corp.example (x"@evil.example) smtp.mailfrom=x; '
                         'dmarc=fail header.from=corp.example')) == (False, False)
    assert flags(own, ar("spf=pass (a) ; dmarc=pass header.from=corp.example (b) smtp.mailfrom=x; dmarc=fail header.from=corp.example")) == (False, False)
    assert flags(own, ar("spf=pass (open ; dkim=pass header.i=@corp.example; dmarc=fail header.from=corp.example")) == (False, False)
    assert flags(own, ar("dkim=pass header.i=x@corp.example@evil.example; dmarc=fail header.from=corp.example")) == (False, False)
    # the same tricks with Google's own quoting of a value, and with a quote the sender put in the comment
    assert flags(own, ar('spf=pass (google.com: domain of dkim=pass+header.i=@corp.example designates 1.2.3.4 as permitted sender) '
                         'smtp.mailfrom="dkim=pass+header.i=@corp.example"; dmarc=fail (p=NONE) header.from=corp.example')) == (False, False)
    assert flags(own, ar('spf=pass smtp.mailfrom="a" ; dkim=pass header.i=@corp.example header.s=s header.b=b; x=y p.q="b@evil.example"; '
                         'dmarc=fail header.from=corp.example')) == (False, False)   # a DKIM part after the SPF part
    assert flags(own, ar('dkim=neutral header.i="a" header.s=q header.b=r; dkim=pass header.i=@corp.example header.s=a header.b=b; '
                         'dkim=neutral header.i="b@evil.example" header.s=s header.b=b; spf=pass smtp.mailfrom=x@evil.example; '
                         'dmarc=fail header.from=corp.example')) == (False, False)   # a quoted header.i
    assert flags(own, ar('dkim=pass header.i=@corp.example header.s=s header.b=b extra.x=1; dmarc=fail header.from=corp.example')) == (False, False)
    assert flags(own, ar('dkim=pass header.i=@corp.example; spf=pass smtp.mailfrom="a b"; dmarc=fail header.from=corp.example')) == (False, False)
    assert flags(own, ar('dkim=pass header.i=@corp.example; spf=pass smtp.mailfrom="a"@evil.example; dmarc=fail header.from=corp.example')) == (False, False)
    assert flags(own, ar('dkim=pass header.i=@corp.example; spf=pass (a \\) b) smtp.mailfrom=x; dmarc=fail header.from=corp.example')) == (False, False)
    assert flags(own, ar('dkim=pass header.i=@corp.example; dmarc=fail header.from=corp.example; dmarc=fail header.from=corp.example')) == (False, False)
    assert flags(own, ar('dkim=pass header.i=@corp.example; dmarc=fail header.from=corp.example extra=1')) == (False, False)
    # a signature of another domain that names ours in its identity
    assert flags(own, ar('dkim=pass header.i=@corp.example header.d=evil.example header.s=s header.b=b; dmarc=fail header.from=corp.example')) == (False, False)
    assert flags(own, ar('dkim=pass header.i=@corp.example header.d=corp.example header.s=s header.b=b; dmarc=fail header.from=corp.example')) == (True, True)
    assert flags(own, ar("dkim=pass header.i=@evil.example header.i=@corp.example; dmarc=fail header.from=corp.example")) == (False, False)
    assert flags(own, ar("dkim=fail header.i=@corp.example; dmarc=fail header.from=corp.example")) == (False, False)
    assert flags(own, ar("dmarc=pass header.from=mx")) == (False, False)
    # a header the sender added does not count: only the first one, from mx.google.com
    assert flags(own, ("Authentication-Results", "mx.evil.example; dmarc=pass header.from=corp.example")) == (False, False)
    assert flags(own, ar("dmarc=fail header.from=corp.example"),
                 ("Authentication-Results", "mx.google.com; dmarc=pass header.from=corp.example")) == (False, False)


def test_outside_sender_can_be_proven_and_is_not_internal():
    assert flags("Ana <ana@client.example>", ar("dkim=pass header.i=@client.example; spf=pass; dmarc=pass header.from=client.example")) == (True, False)
    assert flags("Bot <x@mail.bot.example>", ar("dkim=pass header.i=@bot.example; dmarc=pass header.from=bot.example")) == (True, False)
    assert flags("Ana <ana@client.example>", ar("dkim=pass header.i=@other.example; dmarc=pass header.from=other.example")) == (False, False)


def test_workspace_signature_counts_only_for_the_owners_own_domain():
    # «my.co.com» and «my-co.com» give the same Workspace name, so for an outside domain the name proves nothing
    look = ar("dkim=pass header.i=@my-co-com.20260707.gappssmtp.com; spf=pass smtp.mailfrom=x@my.co.com; dmarc=fail header.from=my-co.com")
    assert flags("Ana <ana@my-co.com>", look) == (False, False)
    assert flags("Ana <ana@client.example>", ar("dkim=pass header.i=@client-example.20260707.gappssmtp.com; dmarc=fail header.from=client.example")) == (False, False)


def test_real_google_header_shape_is_read():
    real = ("Authentication-Results", "mx.google.com;\r\n       dkim=pass header.i=@corp-example.20260707.gappssmtp.com header.s=20260707 header.b=\"Ab+Cd/E=\";\r\n"
            "       arc=pass (i=1);\r\n"
            "       spf=neutral (google.com: 209.85.220.41 is neither permitted nor denied by best guess record for domain of sam@corp.example) smtp.mailfrom=sam@corp.example;\r\n"
            "       dmarc=fail (p=NONE sp=NONE dis=NONE) header.from=corp.example;\r\n       dara=neutral header.i=@corp.example")
    assert flags("Sam <sam@corp.example>", real) == (True, True)
    out = ("Authentication-Results", "mx.google.com; dkim=pass header.i=@client.example header.s=s1 header.b=abc; "
           "dkim=pass header.i=@mailer.example header.s=hsbnp7 header.b=Qw12; "
           "arc=pass (i=1 spf=pass spfdomain=client.example dkim=pass dkdomain=client.example dmarc=pass fromdomain=client.example); "
           "spf=pass (google.com: domain of bounces+1=a@em.client.example designates 2607:f8b0::1 as permitted sender) "
           "smtp.mailfrom=\"bounces+1=a@em.client.example\"; "
           "dmarc=pass (p=REJECT sp=REJECT dis=NONE) header.from=client.example")
    assert flags("Ana <ana@client.example>", out) == (True, False)


def test_control_characters_never_leave_polar():
    m = mail("m1", frm='"Bob\x1b[2J\x03 ' + "N" * 120 + '" <bob@client.example>', subject="Hola\x1b\x03 \r\n ya",
             body="uno\x1b\x03\r\ndos\u202e tres\x07")
    _, out = rows(m)
    r = out[0]
    for field in ("sender", "sender_id", "subject", "text"):
        assert all(ch.isprintable() for ch in r[field]), field
    assert r["subject"] == "Hola ya" and r["text"] == "Hola ya — uno dos tres" and len(r["sender"]) == 80


def paged(msgs):
    """A fake that lists the newest first, in pages, as Gmail does."""
    svc = FakeGmail(msgs)
    newest_first = sorted(msgs, key=lambda m: -int(m["internalDate"]))

    def listing(kw):
        after = int(re.search(r"after:(\d+)", kw["q"]).group(1)) * 1000
        found = [m for m in newest_first if int(m["internalDate"]) >= after]
        start = int(kw.get("pageToken") or 0)
        page = found[start:start + kw["maxResults"]]
        nxt = start + len(page)
        return {"messages": [{"id": m["id"]} for m in page], **({"nextPageToken": str(nxt)} if nxt < len(found) else {})}
    svc.responses["messages.list"] = listing
    return svc


def test_every_page_is_listed_and_all_is_read_when_it_fits():
    svc = paged([mail("m%03d" % i, ms=(T0 + 1 + i) * 1000) for i in range(130)])
    out, more = gmail.inbox(svc, T0, batch=200)
    assert more is None and len(out) == 130 and out[0]["message_id"] == "m000" and out[-1]["message_id"] == "m129"
    assert [kw.get("pageToken") for kw in svc.of("messages.list")] == [None, "100"]


def test_a_long_backlog_is_read_oldest_first_in_batches():
    msgs = [mail("m%03d" % i, ms=(T0 + 1 + i) * 1000) for i in range(130)]
    svc = paged(msgs)
    out, more = gmail.inbox(svc, T0, batch=50)
    assert [r["message_id"] for r in out] == ["m%03d" % i for i in range(50)]
    assert more == T0 + 50 and len(svc.of("messages.get")) == 50   # the newest one opened; the caller asks from there
    out, more = gmail.inbox(svc, more - 1, batch=50)
    assert out[0]["message_id"] == "m049" and out[-1]["message_id"] == "m097" and more == T0 + 98
    out, more = gmail.inbox(svc, more - 1, batch=50)
    assert more is None and out[-1]["message_id"] == "m129"


def test_a_burst_in_one_second_is_opened_whole_so_the_next_read_moves_on():
    burst = [mail("b%02d" % i, ms=T0 * 1000 + 500, thread="B%d" % i) for i in range(7)]
    later = [mail("z%d" % i, ms=(T0 + 10 + i) * 1000) for i in range(3)]
    svc = paged(burst + later)
    out, more = gmail.inbox(svc, T0, batch=3)
    assert sorted(r["message_id"] for r in out)[:7] == ["b%02d" % i for i in range(7)]   # past the batch of 3
    assert [r["message_id"] for r in out][7:] == ["z0"] and more == T0 + 10              # then it ends, after the burst
    out, more = gmail.inbox(svc, more - 1, batch=3)
    assert [r["message_id"] for r in out] == ["z0", "z1", "z2"] and more is None


def test_a_slow_read_stops_after_its_time_and_says_where():
    svc = paged([mail("m%d" % i, ms=(T0 + 1 + i) * 1000) for i in range(5)])
    ticks = iter([0, 1, 30, 31, 32, 33, 34])
    out, more = gmail.inbox(svc, T0, seconds=20, clock=lambda: next(ticks))
    assert [r["message_id"] for r in out] == ["m0", "m1"] and more == T0 + 2


def test_skipped_mail_also_moves_the_mark():
    # 3 newsletters, then a person: the batch of 3 gives no row, and the mark still moves past them
    msgs = [mail("n%d" % i, frm="no-reply@x.com", ms=(T0 + 1 + i) * 1000) for i in range(3)] + [mail("p", ms=(T0 + 9) * 1000)]
    out, more = gmail.inbox(paged(msgs), T0, batch=3)
    assert out == [] and more == T0 + 3


def test_a_read_that_gmail_stops_in_the_middle_keeps_what_it_opened(capsys):
    msgs = [mail("m%d" % i, ms=(T0 + 1 + i) * 1000) for i in range(4)]
    svc = paged(msgs)
    real = svc.responses["messages.get"]

    class Gone(Exception):
        resp = type("R", (), {"status": 404})()

    def get(kw):
        if kw["id"] == "m1":
            raise Gone()                      # deleted after the list: skipped
        if kw["id"] == "m3":
            raise RuntimeError("quota")       # refused: the read ends here
        return real(kw)
    svc.responses["messages.get"] = get
    out, more = gmail.inbox(svc, T0)
    assert [r["message_id"] for r in out] == ["m0", "m2"] and more == T0 + 3
    assert "stopped after 3 messages" in capsys.readouterr().err


def test_a_read_that_gmail_refuses_from_the_start_fails():
    svc = paged([mail("m0")])

    def get(kw):
        raise RuntimeError("quota")
    svc.responses["messages.get"] = get
    try:
        gmail.inbox(svc, T0)
    except RuntimeError:
        return
    raise AssertionError("no error")


def test_over_the_list_cap_says_so(capsys):
    svc = paged([mail("m%03d" % i, ms=(T0 + 1 + i) * 1000) for i in range(250)])
    out, more = gmail.inbox(svc, T0, batch=10, list_max=200)
    assert out[0]["message_id"] == "m050" and more is not None
    assert "the oldest are not read" in capsys.readouterr().err


def test_cli_prints_json_lines(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("POLAR_DATA_DIR", str(tmp_path))
    svc = FakeGmail([mail("m1"), mail("m2", ms=(T0 + 120) * 1000)])
    monkeypatch.setattr(gmail, "service", lambda: svc)
    assert cli.main(["mail", "inbox", "--since", str(T0)]) == 0
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines()]
    assert [l["message_id"] for l in lines] == ["m1", "m2"] and lines[0]["source"] == "mail"


def test_cli_without_gmail_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("POLAR_DATA_DIR", str(tmp_path))
    assert cli.main(["mail", "inbox", "--since", "0"]) == 2
    assert capsys.readouterr().out == ""


def test_cli_says_when_there_is_more(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("POLAR_DATA_DIR", str(tmp_path))
    svc = paged([mail("m%d" % i, ms=(T0 + 1 + i) * 1000) for i in range(4)])
    monkeypatch.setattr(gmail, "service", lambda: svc)
    monkeypatch.setattr(gmail, "INBOX_BATCH", 2)
    monkeypatch.setattr(gmail.inbox, "__defaults__", (2,) + gmail.inbox.__defaults__[1:])
    assert cli.main(["mail", "inbox", "--since", str(T0)]) == 0
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines()]
    assert [l.get("message_id") for l in lines] == ["m0", "m1", "m2", None]   # a batch ends over 2 s after `since`
    assert lines[-1] == {"source": "mail-more", "ts": "%d.000" % (T0 + 3)}
