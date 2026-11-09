from app.server.tenant_match import match

DAV = {"dav_enabled": True, "caldav_ok": True, "carddav_ok": True}
OK = {"imap": {"ok": True}, "caldav": {"ok": True}, "carddav": {"ok": True}}


def mb(mid, mail, upn=None, aliases=(), kind="user"):
    return {"id": mid, "display_name": mid, "mail": mail, "upn": upn or mail, "aliases": list(aliases), "kind": kind,
            "enabled": True, "licensed": True}


def acct(aid, email, **kw):
    return {"id": aid, "email": email, "source_kind": None, "source_connection_id": None, "pst_valid": False,
            "opt_mail": True, "opt_calendar": True, "opt_contacts": True, "last_test_result": OK, "job": None, **kw}


def states(res):
    return {r["address"]: r["state"] for r in res["rows"]}


def run(mailboxes, accounts, server=DAV, tasks=True, tenant="t"):
    return match(mailboxes, accounts, 7, server, tasks, tenant)


def test_six_states_and_counts():
    res = run([mb("a", "a@x.test"), mb("b", "b@x.test"), mb("c", "c@x.test"), mb("d", "d@x.test")],
              [acct(1, "A@x.test"), acct(2, "b@x.test", source_connection_id=7, source_kind="m365_graph"),
               acct(3, "c@x.test", source_kind="imap"), acct(5, "e@x.test")])
    assert states(res) == {"a@x.test": "ready", "b@x.test": "linked", "d@x.test": "no_target",
                           "e@x.test": "no_source", "c@x.test": "other_source"}
    assert [r["state"] for r in res["rows"]] == ["ready", "linked", "no_target", "no_source", "other_source"]
    assert res["counts"] == {"all": 5, "ready": 1, "linked": 1, "no_target": 1, "no_source": 1, "other_source": 1,
                             "conflict": 0}
    assert [r["tickable"] for r in res["rows"]] == [True, True, False, False, False]


def test_case_insensitive_and_precedence():
    # the primary address beats the UPN of another mailbox for the same account
    res = run([mb("p", "p@x.test", upn="shared@x.test"), mb("q", "q@x.test", upn="p@x.test")], [acct(1, "P@X.test")])
    got = {r["mailbox"]["id"]: r for r in res["rows"]}
    assert got["p"]["state"] == "ready" and got["p"]["matched_by"] == "mail"
    assert got["q"]["state"] == "conflict" and got["q"]["matched_by"] == "upn" and not got["q"]["tickable"]
    assert res["counts"]["no_source"] == 0


def test_upn_and_alias_match_and_tie_is_conflict():
    res = run([mb("a", "a@new.test", upn="a@x.test"), mb("b", "b@new.test", aliases=["b@x.test"])],
              [acct(1, "a@x.test"), acct(2, "B@x.test")])
    got = {r["mailbox"]["id"]: r for r in res["rows"]}
    assert (got["a"]["matched_by"], got["b"]["matched_by"], got["b"]["via"]) == ("upn", "alias", "b@x.test")
    tie = run([mb("a", "a@x.test"), mb("b", "b@y.test", aliases=["a@x.test"]), mb("c", "c@y.test", aliases=["a@x.test"])],
              [acct(1, "a@x.test")])
    assert states(tie)["a@x.test"] == "ready"
    assert sorted(r["state"] for r in tie["rows"]) == ["conflict", "conflict", "ready"]
    tie2 = run([mb("b", "b@y.test", aliases=["a@x.test"]), mb("c", "c@y.test", aliases=["a@x.test"])], [acct(1, "a@x.test")])
    assert [r["state"] for r in tie2["rows"]] == ["conflict", "conflict"]


def test_pst_without_valid_file_is_ready_valid_pst_is_other_source():
    res = run([mb("a", "a@x.test"), mb("b", "b@x.test")],
              [acct(1, "a@x.test", source_kind="pst", pst_valid=False), acct(2, "b@x.test", source_kind="pst", pst_valid=True)])
    assert states(res) == {"a@x.test": "ready", "b@x.test": "other_source"}


def test_same_tenant_per_mailbox_source_is_relinkable_other_tenant_is_not():
    res = run([mb("a", "a@x.test"), mb("b", "b@x.test")],
              [acct(1, "a@x.test", source_kind="m365_graph", source_tenant_id="T"),
               acct(2, "b@x.test", source_kind="m365_graph", source_tenant_id="other")])
    assert states(res) == {"a@x.test": "ready", "b@x.test": "other_source"}


def cov(account, server=DAV, tasks=True):
    res = run([mb("a", "a@x.test")], [account], server, tasks)
    return res["rows"][0]["coverage"]


def test_coverage_values():
    assert cov(acct(1, "a@x.test")) == {"mail": "on", "calendar": "on", "contacts": "on", "tasks": "on"}
    assert cov(acct(1, "a@x.test"), {"dav_enabled": False}) == {"mail": "on", "calendar": "no_dav",
                                                                "contacts": "no_dav", "tasks": "no_dav"}
    off = cov(acct(1, "a@x.test", opt_mail=False, opt_calendar=False, opt_contacts=False))
    assert set(off.values()) == {"off"}
    failed = cov(acct(1, "a@x.test", last_test_result={"imap": {"ok": True}, "caldav": {"ok": False},
                                                      "carddav": {"ok": True}}))
    assert (failed["calendar"], failed["tasks"], failed["contacts"]) == ("failed", "failed", "on")
    assert cov(acct(1, "a@x.test"), {"dav_enabled": True, "carddav_ok": False})["contacts"] == "failed"
    untested = cov(acct(1, "a@x.test", last_test_result=None))
    assert untested == {"mail": "on", "calendar": "unknown", "contacts": "unknown", "tasks": "unknown"}
    assert cov(acct(1, "a@x.test"), tasks=False)["tasks"] == "no_permission"
    assert cov(acct(1, "a@x.test", last_test_result={"imap": {"ok": False}}))["mail"] == "failed"


def test_rooms_not_preticked_and_stable_order():
    res = run([mb("z", "z@x.test"), mb("r", "r@x.test", kind="room"), mb("m", "m@x.test")],
              [acct(1, "z@x.test"), acct(2, "r@x.test"), acct(3, "m@x.test")])
    assert [r["address"] for r in res["rows"]] == ["m@x.test", "r@x.test", "z@x.test"]
    assert [r["ticked"] for r in res["rows"]] == [True, False, True]


def test_no_coverage_without_pair():
    res = run([mb("a", "a@x.test")], [acct(1, "b@x.test")])
    assert all(r["coverage"] is None for r in res["rows"])


def test_a_linked_account_keeps_its_mailbox_whatever_its_address():
    # test@y.test was chosen by hand for a: a no longer goes to a@x.test, which is left without source
    res = run([mb("a", "a@x.test"), mb("b", "b@x.test")],
              [acct(1, "a@x.test"), acct(2, "test@y.test", source_connection_id=7, source_kind="m365_graph",
                                         source_mailbox="A")])
    got = {(r["mailbox"] or {}).get("id") or r["account_email"]: r for r in res["rows"]}
    assert (got["a"]["state"], got["a"]["matched_by"], got["a"]["account_id"]) == ("ready", "manual", 2)
    assert got["a"]["ticked"] and got["a"]["coverage"]["mail"] == "on"
    assert got["a@x.test"]["state"] == "no_source" and got["b"]["state"] == "no_target"
    assert res["counts"]["ready"] == 1 and res["counts"]["no_source"] == 1
    # once a job ran: linked
    res = run([mb("a", "a@x.test")], [acct(2, "test@y.test", source_connection_id=7, source_kind="m365_graph",
                                           source_mailbox="a@x.test", job={"id": 1, "status": "done"})])
    assert [(r["state"], r["matched_by"]) for r in res["rows"]] == [("linked", "manual")]


def test_a_linked_account_on_its_own_address_is_linked_as_before():
    res = run([mb("a", "a@x.test", upn="a@corp.test"), mb("b", "b@x.test", aliases=["a@x.test"])],
              [acct(1, "a@x.test", source_connection_id=7, source_kind="m365_graph", source_mailbox="a")])
    got = {r["mailbox"]["id"]: r for r in res["rows"]}
    assert (got["a"]["state"], got["a"]["matched_by"]) == ("linked", "mail")
    assert got["b"]["state"] == "no_target"  # pinned: the alias of b no longer makes a conflict
    # linked to another tenant, or to a mailbox gone from the listing: matched by address as before
    res = run([mb("a", "a@x.test")], [acct(1, "a@x.test", source_connection_id=8, source_kind="m365_graph",
                                           source_mailbox="zz")])
    assert res["rows"][0]["state"] == "other_source"
    res = run([mb("a", "a@x.test")], [acct(1, "a@x.test", source_connection_id=7, source_kind="m365_graph",
                                           source_mailbox="gone")])
    assert (res["rows"][0]["state"], res["rows"][0]["matched_by"]) == ("linked", "mail")
