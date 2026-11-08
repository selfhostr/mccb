"""Exchange inbox rules to Sieve (RFC 5228), and upload over ManageSieve (RFC 5804). Best effort: what has no clean
equivalent is not guessed, it is reported. See docs/exchange.md.

A rule here is {name, enabled, order, conditions, exceptions, actions}. The conditions and actions have the names of
Graph's messageRulePredicates and messageRuleActions, addresses as plain text and folders as paths."""
from __future__ import annotations

import base64
import re
import socket
import ssl
import xml.etree.ElementTree as ET

from .base import ConnectorError

DEFAULT_SCRIPT = "mccb-exchange-rules"
TIMEOUT = 60.0
_connect = socket.create_connection  # replaced in the tests
NOT_MAPPED = {
    "hasAttachments": "has attachments", "headerContains": "any header contains", "importance": "importance",
    "sensitivity": "sensitivity", "categories": "categories", "withinSizeRange": "size range", "sentToMe": "sent to me",
    "sentOnlyToMe": "sent only to me", "sentCcMe": "copy to me", "sentToOrCcMe": "sent or copied to me",
    "notSentToMe": "not sent to me", "isAutomaticForward": "automatic forward", "isAutomaticReply": "automatic reply",
    "isEncrypted": "encrypted", "isMeetingRequest": "meeting request", "isMeetingResponse": "meeting response",
    "isNonDeliveryReport": "non-delivery report", "isPermissionControlled": "permission controlled",
    "isReadReceipt": "read receipt", "isSigned": "signed", "isVoicemail": "voicemail", "isApprovalRequest": "approval",
    "messageActionFlag": "action flag", "other": "other condition",
}


def _addresses(blocks) -> list[str]:
    out = []
    for r in blocks or []:
        addr = ((r or {}).get("emailAddress") or {}).get("address")
        if addr:
            out.append(str(addr))
    return out


def rules_from_graph(rows: list[dict], paths: dict[str, list[str]]) -> list[dict]:
    """`paths`: {folder id: path} of the mailbox."""
    def predicate(p: dict | None) -> dict:
        out: dict = {}
        for k, v in (p or {}).items():
            if k.startswith("@") or v in (None, [], False):
                continue
            out[k] = _addresses(v) if k in ("fromAddresses", "sentToAddresses") else v
        return out
    rules = []
    for r in rows:
        actions: dict = {}
        for k, v in (r.get("actions") or {}).items():
            if v in (None, [], False):
                continue
            if k in ("moveToFolder", "copyToFolder"):
                actions[k] = paths.get(str(v)) or {"unknown": str(v)}
            elif k in ("forwardTo", "redirectTo", "forwardAsAttachmentTo"):
                actions[k] = _addresses(v)
            else:
                actions[k] = v
        rules.append({"name": str(r.get("displayName") or r.get("id") or "rule"), "enabled": bool(r.get("isEnabled", True)),
                      "order": r.get("sequence") or 0, "conditions": predicate(r.get("conditions")),
                      "exceptions": predicate(r.get("exceptions")), "actions": actions})
    return sorted(rules, key=lambda x: x["order"])


_T = "{http://schemas.microsoft.com/exchange/services/2006/types}"
_EWS_STRINGS = {"ContainsSubjectStrings": "subjectContains", "ContainsBodyStrings": "bodyContains",
                "ContainsSubjectOrBodyStrings": "bodyOrSubjectContains", "ContainsSenderStrings": "senderContains",
                "ContainsRecipientStrings": "recipientContains", "ContainsHeaderStrings": "headerContains",
                "Categories": "categories"}
_EWS_ADDRESSES = {"FromAddresses": "fromAddresses", "SentToAddresses": "sentToAddresses"}
_EWS_RECIPIENTS = {"ForwardToRecipients": "forwardTo", "RedirectToRecipients": "redirectTo",
                   "ForwardAsAttachmentToRecipients": "forwardAsAttachmentTo"}
_EWS_FLAGS = {"NotSentToMe": "notSentToMe", "SentCcMe": "sentCcMe", "SentOnlyToMe": "sentOnlyToMe", "SentToMe": "sentToMe",
              "SentToOrCcMe": "sentToOrCcMe", "HasAttachments": "hasAttachments", "IsAutomaticForward": "isAutomaticForward",
              "IsAutomaticReply": "isAutomaticReply", "IsEncrypted": "isEncrypted", "IsMeetingRequest": "isMeetingRequest",
              "IsMeetingResponse": "isMeetingResponse", "IsNDR": "isNonDeliveryReport",
              "IsPermissionControlled": "isPermissionControlled", "IsReadReceipt": "isReadReceipt", "IsSigned": "isSigned",
              "IsVoicemail": "isVoicemail", "IsApprovalRequest": "isApprovalRequest"}


def _ews_predicates(el) -> dict:
    out: dict = {}
    for child in el if el is not None else []:
        name = child.tag.rsplit("}", 1)[-1]
        if name in _EWS_STRINGS:
            values = [s.text for s in child.findall(f"{_T}String") if s.text]
            if values:
                out[_EWS_STRINGS[name]] = values
        elif name in _EWS_ADDRESSES:
            values = [a.findtext(f"{_T}EmailAddress") for a in child.findall(f"{_T}Address")]
            values = [v for v in values if v]
            if values:
                out[_EWS_ADDRESSES[name]] = values
        elif name in _EWS_FLAGS:
            if (child.text or "").strip().lower() == "true":
                out[_EWS_FLAGS[name]] = True
        else:
            out[NOT_MAPPED.get(name[0].lower() + name[1:]) and name[0].lower() + name[1:] or "other"] = True
    return out


def rules_from_ews(resp: ET.Element, paths: dict[str, list[str]]) -> list[dict]:
    """`resp`: the GetInboxRulesResponse element. `paths`: {folder id: path} of the mailbox."""
    rules = []
    for r in resp.iter(f"{_T}Rule"):
        actions: dict = {}
        el = r.find(f"{_T}Actions")
        for child in el if el is not None else []:
            name = child.tag.rsplit("}", 1)[-1]
            if name in ("MoveToFolder", "CopyToFolder"):
                ident = child.find(f"{_T}FolderId")
                fid = ident.get("Id") if ident is not None else ""
                actions["moveToFolder" if name == "MoveToFolder" else "copyToFolder"] = paths.get(fid) or {"unknown": fid}
            elif name in _EWS_RECIPIENTS:
                values = [a.findtext(f"{_T}EmailAddress") for a in child.findall(f"{_T}Address")]
                actions[_EWS_RECIPIENTS[name]] = [v for v in values if v]
            elif name == "Delete" and (child.text or "").lower() == "true":
                actions["delete"] = True
            elif name == "PermanentDelete" and (child.text or "").lower() == "true":
                actions["permanentDelete"] = True
            elif name == "MarkAsRead" and (child.text or "").lower() == "true":
                actions["markAsRead"] = True
            elif name == "StopProcessingRules" and (child.text or "").lower() == "true":
                actions["stopProcessingRules"] = True
            elif name == "AssignCategories":
                actions["assignCategories"] = [s.text for s in child.findall(f"{_T}String") if s.text]
            elif name == "MarkImportance":
                actions["markImportance"] = child.text
            elif name not in ("Delete", "PermanentDelete", "MarkAsRead", "StopProcessingRules"):
                actions[name] = True  # SendSMSAlertToRecipients, ServerReplyWithMessage...: reported as not converted
        priority = r.findtext(f"{_T}Priority")
        rules.append({"name": r.findtext(f"{_T}DisplayName") or "rule",
                      "enabled": (r.findtext(f"{_T}IsEnabled") or "true").lower() == "true",
                      "order": int(priority) if (priority or "").isdigit() else 0,
                      "conditions": _ews_predicates(r.find(f"{_T}Conditions")),
                      "exceptions": _ews_predicates(r.find(f"{_T}Exceptions")), "actions": actions})
    return sorted(rules, key=lambda x: x["order"])


# ------------------------------------------------------------------ Sieve
def _quote(text: str) -> str:
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _list(values: list[str]) -> str:
    return _quote(values[0]) if len(values) == 1 else "[" + ", ".join(_quote(v) for v in values) + "]"


def _tests(pred: dict, needs: set[str]) -> tuple[list[str], list[str]]:
    """Sieve tests of the conditions (they all apply), and the reasons why some cannot be expressed."""
    tests, reasons = [], []
    for key, value in pred.items():
        if key == "subjectContains":
            tests.append(f'header :contains "subject" {_list(value)}')
        elif key == "bodyContains":
            needs.add("body")
            tests.append(f"body :text :contains {_list(value)}")
        elif key == "bodyOrSubjectContains":
            needs.add("body")
            tests.append(f'anyof(header :contains "subject" {_list(value)}, body :text :contains {_list(value)})')
        elif key == "senderContains":
            tests.append(f'address :contains "from" {_list(value)}')
        elif key == "recipientContains":
            tests.append(f'address :contains ["to", "cc"] {_list(value)}')
        elif key == "fromAddresses":
            tests.append(f'address :is "from" {_list(value)}')
        elif key == "sentToAddresses":
            tests.append(f'address :is ["to", "cc"] {_list(value)}')
        else:
            reasons.append(f"condition: {NOT_MAPPED.get(key, key)}")
    return tests, reasons


def _folder(path, delimiter: str, root: str) -> str | None:
    if not isinstance(path, list) or not path:
        return None
    return delimiter.join(([root] if root else []) + [str(p) for p in path])


def convert(rules: list[dict], delimiter: str = "/", root: str = "", trash: str = "Trash") -> dict:
    """{script, converted: [name], partial: [{name, dropped}], not_converted: [{name, reasons}], warnings: [text]}.

    A condition or exception that cannot be expressed drops the rule: without it the rule would match more mail than
    in Exchange. An action that cannot be expressed is dropped and reported, the others stay."""
    needs: set[str] = set()
    blocks: list[str] = []
    converted: list[str] = []
    partial: list[dict] = []
    not_converted: list[dict] = []
    warnings: list[str] = []
    for r in rules:
        name = str(r.get("name") or "rule")
        if not r.get("enabled", True):
            not_converted.append({"name": name, "reasons": ["disabled in Exchange"]})
            continue
        local_needs: set[str] = set()
        tests, reasons = _tests(r.get("conditions") or {}, local_needs)
        exc_tests, exc_reasons = _tests(r.get("exceptions") or {}, local_needs)
        reasons += [f"exception, {x}" for x in exc_reasons]
        tests += [f"not {t}" for t in exc_tests]
        lines: list[str] = []
        dropped: list[str] = []
        actions = r.get("actions") or {}
        if actions.get("markAsRead"):
            local_needs.add("imap4flags")
            lines.append('addflag "\\\\Seen";')
        for key in ("forwardTo", "redirectTo"):
            if actions.get(key):
                local_needs.add("copy")
                lines.extend(f"redirect :copy {_quote(a)};" for a in actions[key])
                warnings.append(f"rule '{name}' forwards mail to {', '.join(actions[key])}: check before activating")
        if "copyToFolder" in actions:
            target = _folder(actions["copyToFolder"], delimiter, root)
            if target is None:
                dropped.append("copy to a folder that was not found")
            else:
                local_needs.update(("copy", "fileinto"))
                lines.append(f"fileinto :copy {_quote(target)};")
        if "moveToFolder" in actions:
            target = _folder(actions["moveToFolder"], delimiter, root)
            if target is None:
                # a rule meant to file mail away, not doing so, is not the same rule
                reasons.append("moves to a folder that was not found")
            else:
                local_needs.add("fileinto")
                lines.append(f"fileinto {_quote(target)};")
        if actions.get("delete"):
            local_needs.add("fileinto")
            lines.append(f"fileinto {_quote(trash)};")
        if actions.get("permanentDelete"):
            lines.append("discard;")
        if actions.get("stopProcessingRules"):
            lines.append("stop;")
        known = {"markAsRead", "forwardTo", "redirectTo", "copyToFolder", "moveToFolder", "delete", "permanentDelete",
                 "stopProcessingRules"}
        for key, value in actions.items():
            if key not in known and value not in (None, [], False):
                dropped.append({"markImportance": "mark importance", "assignCategories": "assign categories",
                                "forwardAsAttachmentTo": "forward as attachment"}.get(key, key))
        if not [x for x in lines if x != "stop;"]:
            reasons.append("no action has a Sieve equivalent")
        if reasons:
            not_converted.append({"name": name, "reasons": reasons})
            continue
        needs |= local_needs
        clean = re.sub(r"[\r\n]+", " ", name)
        cond = "true" if not tests else tests[0] if len(tests) == 1 else "allof(" + ", ".join(tests) + ")"
        blocks.append(f"# {clean}\nif {cond} {{\n" + "".join(f"    {x}\n" for x in lines) + "}\n")
        if dropped:
            partial.append({"name": name, "dropped": dropped})
        else:
            converted.append(name)
    head = "# Converted from Exchange inbox rules by mccb. Best effort: read it before activating it.\n"
    if needs:
        head += f"require {'[' + ', '.join(_quote(n) for n in sorted(needs)) + ']'};\n"
    return {"script": head + "\n" + "\n".join(blocks), "converted": converted, "partial": partial,
            "not_converted": not_converted, "warnings": warnings}


# ------------------------------------------------------------------ ManageSieve
class ManageSieve:
    """Minimal client: capabilities, STARTTLS, AUTHENTICATE PLAIN, PUTSCRIPT, SETACTIVE, LOGOUT."""

    def __init__(self, host: str, port: int = 4190, security: str = "starttls", verify_tls: bool = True):
        self.host, self.port, self.security, self.verify_tls = host, int(port), security, verify_tls
        self.sock = None
        self.f = None
        self.caps: dict[str, str] = {}

    def _ctx(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        if not self.verify_tls:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _wrap(self) -> None:
        self.sock = self._ctx().wrap_socket(self.sock, server_hostname=self.host)
        self.f = self.sock.makefile("rb")

    def _read_response(self) -> tuple[str, str, list[str]]:
        """(OK | NO | BYE, text after it, the lines before it)."""
        lines: list[str] = []
        while True:
            raw = self.f.readline(65536)
            if not raw:
                raise ConnectorError("ManageSieve: connection closed", ConnectorError.TRANSIENT)
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            m = re.match(r"^(OK|NO|BYE)\b\s*(.*)$", line, re.I)
            if m:
                return m.group(1).upper(), m.group(2), lines
            literal = re.search(r"\{(\d+)\+?\}$", line)
            if literal:  # a string sent as a literal: not needed here, only its bytes are skipped
                self.f.read(int(literal.group(1)))
            lines.append(line)

    def _capabilities(self, lines: list[str]) -> None:
        self.caps = {}
        for line in lines:
            m = re.match(r'^"([^"]+)"(?:\s+"([^"]*)")?', line)
            if m:
                self.caps[m.group(1).upper()] = m.group(2) or ""

    def connect(self) -> None:
        try:
            self.sock = _connect((self.host, self.port), TIMEOUT)
            if self.security == "ssl":
                self._wrap()
            else:
                self.f = self.sock.makefile("rb")
            status, text, lines = self._read_response()
            if status != "OK":
                raise ConnectorError(f"ManageSieve refused: {text}", ConnectorError.UNREACHABLE)
            self._capabilities(lines)
            if self.security == "starttls":
                if "STARTTLS" not in self.caps:
                    raise ConnectorError("ManageSieve: the server does not offer STARTTLS", ConnectorError.CONFIG)
                self.sock.sendall(b"STARTTLS\r\n")
                status, text, _ = self._read_response()
                if status != "OK":
                    raise ConnectorError(f"ManageSieve STARTTLS refused: {text}", ConnectorError.CONFIG)
                self._wrap()
                _status, _text, lines = self._read_response()
                self._capabilities(lines)
        except OSError as e:
            self.close()
            raise ConnectorError(f"ManageSieve {self.host}:{self.port}: {e}", ConnectorError.UNREACHABLE) from e

    def login(self, username: str, password: str) -> None:
        if "PLAIN" not in self.caps.get("SASL", "").upper().split():
            raise ConnectorError("ManageSieve: the server does not offer SASL PLAIN", ConnectorError.CONFIG)
        blob = base64.b64encode(b"\0" + username.encode("utf-8") + b"\0" + password.encode("utf-8")).decode("ascii")
        status, text = self._command(f'AUTHENTICATE "PLAIN" "{blob}"')
        if status != "OK":
            raise ConnectorError(f"ManageSieve login refused: {text}", ConnectorError.AUTH)

    def _command(self, line: str) -> tuple[str, str]:
        try:
            self.sock.sendall(line.encode("utf-8") + b"\r\n")
            status, text, _ = self._read_response()
        except OSError as e:
            raise ConnectorError(f"ManageSieve: {e}", ConnectorError.TRANSIENT) from e
        return status, text

    def put_script(self, name: str, script: str) -> None:
        data = script.encode("utf-8")
        try:
            self.sock.sendall(f'PUTSCRIPT {_quote(name)} {{{len(data)}+}}\r\n'.encode("utf-8") + data + b"\r\n")
            status, text, _ = self._read_response()
        except OSError as e:
            raise ConnectorError(f"ManageSieve: {e}", ConnectorError.TRANSIENT) from e
        if status != "OK":
            raise ConnectorError(f"ManageSieve refused the script: {text}", ConnectorError.REJECTED)

    def set_active(self, name: str) -> None:
        status, text = self._command(f"SETACTIVE {_quote(name)}")
        if status != "OK":
            raise ConnectorError(f"ManageSieve refused to activate the script: {text}", ConnectorError.REJECTED)

    def close(self) -> None:
        sock, self.sock = self.sock, None
        if sock is None:
            return
        try:
            sock.sendall(b"LOGOUT\r\n")
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass


def upload(script: str, host: str, username: str, password: str, port: int = 4190, security: str = "starttls",
           verify_tls: bool = True, name: str = DEFAULT_SCRIPT, activate: bool = False) -> None:
    """Stores the script. It only runs if activated, which replaces the active script of the user: off by default."""
    client = ManageSieve(host, port, security, verify_tls)
    try:
        client.connect()
        client.login(username, password)
        client.put_script(name, script)
        if activate:
            client.set_active(name)
    finally:
        client.close()
