"""Simulierter IMAP-Server für Tests (gleiche Schnittstelle wie imaplib.IMAP4, soweit benutzt)."""
import re

from app.services.imap import utf7_encode


class FakeIMAP:
    def __init__(self, folders: list[str], *, delimiter: str = ".", capabilities=("IMAP4REV1", "MOVE", "UIDPLUS"),
                 special: dict[str, str] | None = None):
        self.capabilities = tuple(capabilities)
        self.delimiter = delimiter
        self.boxes: dict[str, list[dict]] = {utf7_encode(f): [] for f in folders}
        self.special = {utf7_encode(k): v for k, v in (special or {}).items()}
        self.subscribed: set[str] = set()
        self.selected: str | None = None
        self.next_uid = 1
        self.drop_copies = False   # simuliert: COPY meldet OK, kommt aber nicht an
        self.logged_in = False
        self.commands: list[str] = []

    # ---------------------------------------------------------- Hilfen
    @staticmethod
    def _name(mailbox: str) -> str:
        if mailbox.startswith('"') and mailbox.endswith('"'):
            mailbox = mailbox[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        return mailbox

    def add_mail(self, folder: str, data: bytes, flags: set[str] | None = None) -> int:
        uid = self.next_uid
        self.next_uid += 1
        self.boxes[utf7_encode(folder)].append({"uid": uid, "data": data, "flags": set(flags or ())})
        return uid

    def mails(self, folder: str) -> list[dict]:
        return self.boxes.get(utf7_encode(folder), [])

    def _find(self, uid: int) -> dict | None:
        return next((m for m in self.boxes[self.selected] if m["uid"] == uid), None)

    @staticmethod
    def _uids(spec) -> set[int]:
        spec = spec.decode() if isinstance(spec, bytes) else str(spec)
        return {int(x) for x in spec.split(",") if x}

    # ---------------------------------------------------------- IMAP-Befehle
    def login(self, user, password):
        self.logged_in = True
        return "OK", [b"Logged in"]

    def logout(self):
        return "BYE", [b""]

    def list(self):
        lines = []
        for name in self.boxes:
            flags = "\\HasNoChildren" + (f" {self.special[name]}" if name in self.special else "")
            lines.append(f'({flags}) "{self.delimiter}" "{name}"'.encode())
        return "OK", lines

    def select(self, mailbox, readonly=False):
        name = self._name(mailbox)
        if name not in self.boxes:
            return "NO", [b"Mailbox does not exist"]
        self.selected = name
        return "OK", [str(len(self.boxes[name])).encode()]

    def create(self, mailbox):
        self.boxes.setdefault(self._name(mailbox), [])
        self.commands.append(f"CREATE {self._name(mailbox)}")
        return "OK", [b""]

    def subscribe(self, mailbox):
        self.subscribed.add(self._name(mailbox))
        return "OK", [b""]

    def append(self, mailbox, flags, date_time, message):
        name = self._name(mailbox)
        if name not in self.boxes:
            return "NO", [b"Mailbox does not exist"]
        uid = self.next_uid
        self.next_uid += 1
        self.boxes[name].append({"uid": uid, "data": message, "flags": set(re.findall(r"\\\w+", flags))})
        return "OK", [b"APPEND completed"]

    def expunge(self):  # darf nie benutzt werden (würde fremde gelöschte Mails mitnehmen)
        raise AssertionError("EXPUNGE ohne UID darf nicht verwendet werden")

    def uid(self, command, *args):
        command = command.upper()
        self.commands.append(command)
        box = self.boxes[self.selected]
        if command == "SEARCH":
            return "OK", [b" ".join(str(m["uid"]).encode() for m in box)]
        if command == "FETCH":
            mail = self._find(int(args[0]))
            if mail is None:
                return "OK", [None]
            data = mail["data"]
            if "HEADER" in args[1]:
                data = data.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n" if b"\r\n\r\n" in data else data.split(b"\n\n")[0]
            return "OK", [(f"1 (UID {mail['uid']} BODY[] {{{len(data)}}}".encode(), data), b")"]
        if command in ("MOVE", "COPY"):
            if "MOVE" == command and "MOVE" not in self.capabilities:
                return "NO", [b"unknown command"]
            mail = self._find(int(args[0]))
            target = self._name(args[1])
            if mail is None or target not in self.boxes:
                return "NO", [b"failed"]
            if not (command == "COPY" and self.drop_copies):
                self.boxes[target].append({"uid": self.next_uid, "data": mail["data"], "flags": set()})
                self.next_uid += 1
            if command == "MOVE":
                box.remove(mail)
            return "OK", [b""]
        if command == "STORE":
            for uid in self._uids(args[0]):
                mail = self._find(uid)
                if mail:
                    mail["flags"].add("\\Deleted")
            return "OK", [b""]
        if command == "EXPUNGE":
            if "UIDPLUS" not in self.capabilities:
                return "NO", [b"unknown command"]
            uids = self._uids(args[0])
            self.boxes[self.selected] = [m for m in box if not (m["uid"] in uids and "\\Deleted" in m["flags"])]
            return "OK", [b""]
        raise AssertionError(f"Unerwarteter Befehl {command}")
