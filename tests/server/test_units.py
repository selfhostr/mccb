from app.server.imap import Mailbox, find_special, parse_list, utf7_decode, utf7_encode
from app.server.runner import norm_hash, plan_folders
from tests.server.fake_engine import FolderInfo


def test_utf7_roundtrip():
    for s in ["Éléments envoyés", "Boîte de réception", "R&D", "日本語", "Été 2024 – Projets", "plain"]:
        assert utf7_decode(utf7_encode(s)) == s
    assert utf7_encode("Éléments envoyés") == "&AMk-l&AOk-ments envoy&AOk-s"
    assert utf7_encode("R&D") == "R&-D"


def test_parse_list_and_special_use():
    data = [b'(\\HasNoChildren) "/" INBOX', b'(\\HasNoChildren \\Sent) "/" "Envoy&AOk-s"',
            b'(\\Trash) "/" "Deleted Items"', (b'(\\Junk) "/" {4}', b'Spam'), b'(\\Noselect) NIL "Root"']
    boxes = parse_list(data)
    assert [b.name for b in boxes] == ["INBOX", "Envoy&AOk-s", "Deleted Items", "Spam", "Root"]
    assert boxes[1].decoded == "Envoyés" and boxes[4].delim is None
    assert find_special(boxes, "sent") == "Envoy&AOk-s"
    assert find_special(boxes, "junk") == "Spam"
    assert find_special(boxes, "drafts") is None
    assert find_special([Mailbox("Drafts", [], "/")], "drafts") == "Drafts"


def test_plan_folders():
    fs = [FolderInfo(["Boîte de réception"], "mail", "IPF.Note", "inbox", 1),
          FolderInfo(["Boîte de réception", "Sous/dossier"], "mail", "IPF.Note", None, 1),
          FolderInfo(["Éléments envoyés"], "mail", "IPF.Note", "sent", 1),
          FolderInfo(["Brouillons"], "mail", "IPF.Note", "drafts", 1),
          FolderInfo(["Archives"], "mail", "IPF.Note", None, 1)]
    boxes = [Mailbox("INBOX", [], "."), Mailbox("INBOX.Sent", ["\\Sent"], ".")]
    p = plan_folders(fs, boxes, ".", None, "INBOX.")
    assert p["Boîte de réception"] == ("INBOX", None)
    assert p["Boîte de réception/Sous\\/dossier"][0] == "INBOX.Sous/dossier"
    assert p["Éléments envoyés"] == ("INBOX.Sent", None)
    assert p["Brouillons"] == ("INBOX.Drafts", "\\Drafts")
    assert p["Archives"][0] == "INBOX.Archives"
    p = plan_folders(fs, boxes, "/", "PST Import", "")
    assert p["Boîte de réception"][0] == "PST Import/Bo&AO4-te de r&AOk-ception"
    assert p["Boîte de réception/Sous\\/dossier"][0] == "PST Import/Bo&AO4-te de r&AOk-ception/Sous_dossier"


def test_norm_hash_line_endings():
    assert norm_hash(b"a\r\nb\r\n") == norm_hash(b"a\nb\n") == norm_hash(b"a\r\nb")
