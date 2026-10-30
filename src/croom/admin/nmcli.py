"""
Wi-Fi through NetworkManager's nmcli (room settings spec 2026-10-29, sections 3 and 7): what the TV Pi's root helper (croom.admin.wifi)
and the touch Pi's agent (tools/kiosk_agent, which loads this file by its path) both do to look for networks, join one, forget one and
say how a device is connected. Standard-library Python only, and nothing of croom is imported: the touch Pi runs it with the system's
python3, outside any venv.

nmcli is run with argument lists (never a shell), in the C locale (env LC_ALL=C LANG=C: its state words and errors are matched in
English), in its terse mode (-t -f FIELDS), whose values escape ":" and "\\" with a backslash
(split_terse). The password never goes on a command line, where any user could read it in the process list: a new network's profile
is written by this module as a NetworkManager keyfile, root's and mode 600, in NetworkManager's own folder, and loaded with `nmcli
connection load`; it is then brought up by its UUID.

The safety net (spec section 7): a change keeps the connection the device was on (its UUID), writes down what it is about to do before
it does anything (the record: root's alone, through a `store` the caller gives), joins the new network with a profile that does not
connect by itself (autoconnect off), and gives the device TRIAL_S (90 s) to show it can do its job there: the checks the caller gives
(the TV Pi: it reaches its gateway and sees the touch Pi check in; the touch Pi: it reaches the room's GET /api/status). Proven, the new
profile is made to connect by itself and the profiles it replaces (the same network's, with an old password) go; not proven, the old
connection is brought up again and the new profile deleted, and the record says why. A power cut at any moment leaves a record that
recover() finishes (proven: kept) or undoes (anything before: back to the old connection), and until then the new profile, which does
not connect by itself, cannot keep the device on a network that did not work. The old profile is never deleted by a change.
"""

import ipaddress
import os
import time
import uuid as uuid_module
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

Runner = Callable[[List[str]], Tuple[int, str]]

TRIAL_S = 90                      # a new network has this long to prove the device can do its job on it
CHECK_EVERY_S = 5
CONNECT_WAIT_S = 45               # nmcli --wait: how long joining may take (DHCP included)
SCAN_CACHE_S = 20                 # a scan is reused this long (Join looks a network up in it)
MAX_NETWORKS = 30
UNDER_WAY = ("trying", "proven", "undoing")
C_LOCALE = ["env", "LC_ALL=C", "LANG=C"]   # every command runs in the C locale: the words matched here are the ones nmcli prints
SSID_WORDS = "A network's name is 1 to 32 characters"
PASSWORD_WORDS = "A Wi-Fi password is 8 to 63 letters, digits and symbols, or a key of 64 characters 0 to 9 and A to F"
WIFI_TYPES = ("wifi", "802-11-wireless")


class WifiError(Exception):
    """A Wi-Fi job refused or failed, in words for the person at the screen."""


# --- checks of what a person types

def check_ssid(ssid: Any) -> str:
    """A network's name: text of 1 to 32 bytes (UTF-8), no control characters, not only spaces."""
    if not isinstance(ssid, str) or not ssid.strip() or any(ord(c) < 32 or ord(c) == 127 for c in ssid):
        raise WifiError(SSID_WORDS)
    try:
        size = len(ssid.encode("utf-8"))
    except UnicodeEncodeError as e:
        raise WifiError(SSID_WORDS) from e
    if size > 32:
        raise WifiError(SSID_WORDS)
    return ssid


def check_password(password: Any) -> str:
    """"" (an open network), a WPA passphrase (8 to 63 printable ASCII characters) or a raw key (64 hexadecimal digits)."""
    if password is None or password == "":
        return ""
    if not isinstance(password, str):
        raise WifiError(PASSWORD_WORDS)
    if len(password) == 64 and all(c in "0123456789abcdefABCDEF" for c in password):
        return password
    if 8 <= len(password) <= 63 and all(32 <= ord(c) <= 126 for c in password):
        return password
    raise WifiError(PASSWORD_WORDS)


# --- nmcli's terse output

def split_terse(line: str) -> List[str]:
    """One line of `nmcli -t` as its fields: split at each ":" that is not escaped, with "\\:" and "\\\\" read as ":" and "\\"."""
    fields, current, escaped = [], [], False
    for ch in line:
        if escaped:
            current.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(ch)
    fields.append("".join(current))
    return fields


def rows(output: str, count: int) -> List[List[str]]:
    """The lines of terse output that have `count` fields (a line that does not is passed over)."""
    found = []
    for line in (output or "").splitlines():
        if not line.strip():
            continue
        fields = split_terse(line)
        if len(fields) == count:
            found.append(fields)
    return found


def parse_values(output: str) -> Dict[str, str]:
    """`nmcli -t -f A,B device show X` (or `connection show`): {"IP4.ADDRESS[1]": "192.168.1.20/24", "IP4.GATEWAY": ...}. A field's
    name has no ":" in it: the line is split at the first, and the value's escapes read."""
    values = {}
    for line in (output or "").splitlines():
        name, colon, value = line.partition(":")
        if colon and name.strip():
            values[name.strip()] = "".join(split_terse(value)) if "\\" in value else value
    return values


def security_kind(security: str) -> str:
    """What a network's SECURITY (as nmcli lists it: "WPA2", "WPA1 WPA2", "WPA3", "WPA2 802.1X", "WEP", "OWE", "") asks of a device:
    open, owe (open, encrypted), psk (a WPA password), sae (WPA3 alone: a password), wep, or enterprise (a user name)."""
    tokens = (security or "").replace("--", "").split()
    if "802.1X" in tokens:
        return "enterprise"
    if "WEP" in tokens:
        return "wep"
    if "WPA1" in tokens or "WPA2" in tokens:
        return "psk"
    if "WPA3" in tokens:
        return "sae"
    if "OWE" in tokens:
        return "owe"
    return "open" if not tokens else "psk"


def parse_scan(output: str) -> List[Dict[str, Any]]:
    """`nmcli -t -f IN-USE,SSID,SIGNAL,SECURITY device wifi list`: one entry per network name (the strongest of its access points),
    strongest first; networks that hide their name are left out (they are joined by a typed name)."""
    best: Dict[str, Dict[str, Any]] = {}
    for in_use, ssid, signal, security in rows(output, 4):
        if not ssid:
            continue
        try:
            strength = max(0, min(100, int(signal)))
        except ValueError:
            strength = 0
        kind = security_kind(security)
        entry = {"ssid": ssid, "signal": strength, "security": security.strip(" -")[:40], "kind": kind,
                 "open": kind in ("open", "owe"), "in_use": in_use.strip() == "*"}
        known = best.get(ssid)
        if known is None or strength > known["signal"]:
            entry["in_use"] = entry["in_use"] or bool(known and known["in_use"])
            best[ssid] = entry
        elif entry["in_use"]:
            known["in_use"] = True
    return sorted(best.values(), key=lambda n: (-n["signal"], n["ssid"].lower()))[:MAX_NETWORKS]


def parse_devices(output: str) -> List[Dict[str, str]]:
    """`nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status`."""
    return [{"device": d, "type": t, "state": s, "connection": c} for d, t, s, c in rows(output, 4)]


def why_words(output: str) -> str:
    """What nmcli said went wrong, on one line, in words a person can use."""
    text = " ".join(line.strip() for line in (output or "").splitlines() if line.strip())
    if "Secrets were required" in text:
        return "the password was not accepted"
    if "No network with SSID" in text:
        return "the network was not found"
    for prefix in ("Error: ", "Error:"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text[:200] or "nmcli did not say why"


# --- the keyfile

def _kf(value: str) -> str:
    """A string value as GLib's key files read it: a backslash doubled, every space as \\s (a leading or trailing one would be lost)."""
    return value.replace("\\", "\\\\").replace(" ", "\\s")


def keyfile(connection_uuid: str, ssid: str, password: str, kind: str, hidden: bool) -> str:
    """A NetworkManager keyfile for `ssid`: not connecting by itself (autoconnect is turned on once the network is proven), the SSID as
    its bytes (a name with any character in it reads back as it is), the password for a WPA network."""
    lines = ["[connection]", f"id={_kf(ssid)}", f"uuid={connection_uuid}", "type=wifi", "autoconnect=false", "",
             "[wifi]", "mode=infrastructure", "ssid=" + "".join(f"{b};" for b in ssid.encode("utf-8"))]
    if hidden:
        lines.append("hidden=true")
    lines.append("")
    if kind in ("psk", "sae"):
        lines += ["[wifi-security]", f"key-mgmt={'sae' if kind == 'sae' else 'wpa-psk'}", f"psk={_kf(password)}", ""]
    elif kind == "owe":
        lines += ["[wifi-security]", "key-mgmt=owe", ""]
    lines += ["[ipv4]", "method=auto", "", "[ipv6]", "method=auto", ""]
    return "\n".join(lines)


def write_private(path: Path, text: str) -> None:
    """Write a file for root alone (mode 600, a new file, not through a link), flushed to the card with its folder."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        folder = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(folder)
    except OSError:
        pass
    finally:
        os.close(folder)


class Check:
    """One thing a device must show on a new network: `test()` True once it has (it is asked every CHECK_EVERY_S until it is), and
    `failure`, the words for when it has not within TRIAL_S ("the TV Pi could not reach its gateway")."""

    def __init__(self, test: Callable[[], bool], failure: str):
        self.test = test
        self.failure = failure


class Plan:
    """A change to make, checked before anything changes (Wifi.prepare)."""

    def __init__(self, **fields: Any):
        self.__dict__.update(fields)


class Wifi:
    """
    One device's Wi-Fi.

    Args:
        run: runs a command (an argument list): (exit status, output).
        store: the record of a change, root's alone: `load()` gives it ({} when none), `save(record)` writes it whole.
        connections: NetworkManager's keyfile folder (/etc/NetworkManager/system-connections).
        name: the device as the words name it ("TV Pi", "touch Pi").
        clock: wall-clock seconds (the record's times, and when a change was made, for the checks that ask "since").
        monotonic, sleep: the 90 seconds.
        new_uuid: a new profile's UUID.
    """

    def __init__(self, run: Runner, store: Any, connections: Path, name: str, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 new_uuid: Callable[[], str] = lambda: str(uuid_module.uuid4())):
        self.run = lambda args: run(C_LOCALE + list(args))
        self.store = store
        self.connections = Path(connections)
        self.name = name
        self.clock = clock
        self.monotonic = monotonic
        self.sleep = sleep
        self.new_uuid = new_uuid
        self._scan: Optional[Tuple[float, str, List[Dict[str, Any]]]] = None   # (when, device, networks)

    # --- what the device has

    def _nmcli(self, *args: str) -> str:
        code, output = self.run(["nmcli", *args])
        if code != 0:
            raise WifiError(f"nmcli could not answer: {why_words(output)}")
        return output

    def devices(self) -> List[Dict[str, str]]:
        return parse_devices(self._nmcli("-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"))

    @staticmethod
    def wifi_device(devices: List[Dict[str, str]]) -> Optional[Dict[str, str]]:
        return next((d for d in devices if d["type"] == "wifi"), None)

    @staticmethod
    def on_ethernet(devices: List[Dict[str, str]]) -> Optional[Dict[str, str]]:
        return next((d for d in devices if d["type"] == "ethernet" and d["state"].startswith("connected")), None)

    def active(self) -> List[Dict[str, str]]:
        output = self._nmcli("-t", "-f", "NAME,UUID,TYPE,DEVICE", "connection", "show", "--active")
        return [{"name": n, "uuid": u, "type": t, "device": d} for n, u, t, d in rows(output, 4)]

    def profiles(self) -> List[Dict[str, str]]:
        """The Wi-Fi profiles saved on the device, each with its network's name."""
        output = self._nmcli("-t", "-f", "NAME,UUID,TYPE", "connection", "show")
        found = []
        for name, uuid, kind in rows(output, 3):
            if kind not in WIFI_TYPES:
                continue
            code, values = self.run(["nmcli", "-t", "-f", "802-11-wireless.ssid", "connection", "show", "uuid", uuid])
            ssid = parse_values(values).get("802-11-wireless.ssid", "") if code == 0 else ""
            found.append({"name": name, "uuid": uuid, "ssid": ssid or name})
        return found

    def address(self, device: str) -> Optional[str]:
        code, output = self.run(["nmcli", "-t", "-f", "IP4.ADDRESS", "device", "show", device])
        if code != 0:
            return None
        for name, value in parse_values(output).items():
            if name.startswith("IP4.ADDRESS") and value:
                return value.split("/")[0]
        return None

    def gateway(self, device: str) -> Optional[str]:
        code, output = self.run(["nmcli", "-t", "-f", "IP4.GATEWAY", "device", "show", device])
        value = parse_values(output).get("IP4.GATEWAY", "") if code == 0 else ""
        try:
            return str(ipaddress.IPv4Address(value.strip()))
        except ValueError:
            return None

    def status(self) -> Dict[str, Any]:
        """How the device is connected: {"wifi": it has Wi-Fi, "device", "connection": "ethernet" | "wifi" | "none", "ssid", "signal"
        (0 to 100), "address", "saved": the networks it knows, "change": the last change (public_record)}."""
        devices = self.devices()
        wifi, ethernet = self.wifi_device(devices), self.on_ethernet(devices)
        answer: Dict[str, Any] = {"wifi": wifi is not None, "device": wifi["device"] if wifi else None, "connection": "none",
                                  "ssid": None, "signal": None, "address": None, "saved": [], "change": self.public_record()}
        if wifi and wifi["state"].startswith("connected"):
            code, output = self.run(["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list", "--rescan", "no",
                                     "ifname", wifi["device"]])
            here = next((n for n in parse_scan(output) if n["in_use"]), None) if code == 0 else None
            answer.update(ssid=here["ssid"] if here else wifi["connection"], signal=here["signal"] if here else None)
        primary = ethernet or (wifi if wifi and wifi["state"].startswith("connected") else None)
        if primary:
            answer["connection"] = "ethernet" if primary is ethernet else "wifi"
            answer["address"] = self.address(primary["device"])
        if wifi:
            answer["saved"] = sorted({p["ssid"] for p in self.profiles()}, key=str.lower)
        return answer

    def scan(self, fresh: bool = True) -> Dict[str, Any]:
        """The networks in range, strongest first, each marked when the device knows it ("saved"): {"networks", "at"}. A scan within
        SCAN_CACHE_S is reused."""
        wifi = self.wifi_device(self.devices())
        if wifi is None:
            raise WifiError(f"This {self.name} has no Wi-Fi")
        now = self.monotonic()
        if self._scan and self._scan[1] == wifi["device"] and now - self._scan[0] < SCAN_CACHE_S:
            networks = self._scan[2]
        else:
            base = ["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list", "--rescan"]
            code, output = self.run(base + ["yes" if fresh else "auto", "ifname", wifi["device"]])
            if code != 0:   # "Scanning not allowed immediately following previous scan": what it found last will do
                code, output = self.run(base + ["no", "ifname", wifi["device"]])
            if code != 0:
                raise WifiError(f"Could not look for networks: {why_words(output)}")
            networks = parse_scan(output)
            self._scan = (now, wifi["device"], networks)
        saved = {p["ssid"] for p in self.profiles()}
        return {"networks": [dict(n, saved=n["ssid"] in saved) for n in networks], "at": self._iso()}

    # --- the record

    def _iso(self) -> str:
        return datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(timespec="seconds")

    def record(self) -> Dict[str, Any]:
        record = self.store.load()
        return record if isinstance(record, dict) else {}

    def _save(self, record: Dict[str, Any], **changes: Any) -> Dict[str, Any]:
        record.update(changes, at=self._iso())
        self.store.save(dict(record))
        return record

    def public_record(self) -> Optional[Dict[str, Any]]:
        """The last change as a page shows it: {"state", "ssid", "words", "at"}; None before any."""
        record = self.record()
        if not record.get("state"):
            return None
        return {key: record.get(key) for key in ("state", "ssid", "words", "at")}

    def under_way(self) -> bool:
        return self.record().get("state") in UNDER_WAY

    # --- a change

    def prepare(self, ssid: Any, password: Any, hidden: Any = False) -> Plan:
        """Check a change before anything changes: the name and password, the network (in range, unless it is joined as a hidden one;
        a password when it asks for one; not one that needs a user name), the device's Wi-Fi and the connection it is on. WifiError says
        why not."""
        ssid, password, hidden = check_ssid(ssid), check_password(password), bool(hidden)
        if self.under_way():
            raise WifiError("A Wi-Fi change is under way: wait for it to finish")
        devices = self.devices()
        wifi = self.wifi_device(devices)
        if wifi is None:
            raise WifiError(f"This {self.name} has no Wi-Fi")
        profiles = [p for p in self.profiles() if p["ssid"] == ssid]
        seen = next((n for n in self.scan(fresh=False)["networks"] if n["ssid"] == ssid), None)
        if seen is not None:
            kind = seen["kind"]
        elif hidden:
            kind = "psk" if password else "open"
        else:
            raise WifiError(f"{ssid} was not found: look again, or join it as a hidden network")
        if kind == "enterprise":
            raise WifiError(f"{ssid} asks for a user name: networks like it are not supported")
        if kind == "wep":
            raise WifiError(f"{ssid} uses WEP, which is not safe: networks like it are not supported")
        if kind in ("open", "owe") and password:
            raise WifiError(f"{ssid} is an open network: it takes no password")
        if kind == "sae" and len(password) == 64:
            raise WifiError(f"{ssid} takes its password, not a key of 64 characters")
        reuse = profiles[0]["uuid"] if profiles and not password else None
        reuse_autoconnect = self.autoconnect(reuse) if reuse else None
        if kind in ("psk", "sae") and not password and reuse is None:
            raise WifiError(f"Enter the password for {ssid}")
        old = next((a for a in self.active() if a["device"] == wifi["device"]), None)
        if old is not None and not password and any(p["uuid"] == old["uuid"] for p in profiles):
            raise WifiError(f"The {self.name} is on {ssid} already")
        return Plan(ssid=ssid, password=password, hidden=hidden, kind=kind, device=wifi["device"],
                    ethernet=self.on_ethernet(devices) is not None, old=old["uuid"] if old else None,
                    old_name=old["name"] if old else None, reuse=reuse, reuse_autoconnect=reuse_autoconnect,
                    superseded=[p["uuid"] for p in profiles] if not reuse else [])

    def autoconnect(self, connection_uuid: str) -> bool:
        """Whether a saved profile connects by itself (when nmcli does not say: yes, NetworkManager's default)."""
        code, output = self.run(["nmcli", "-t", "-f", "connection.autoconnect", "connection", "show", "uuid", connection_uuid])
        return not (code == 0 and parse_values(output).get("connection.autoconnect", "").strip() == "no")

    def _set_autoconnect(self, connection_uuid: str, on: bool) -> Tuple[int, str]:
        return self.run(["nmcli", "connection", "modify", "uuid", connection_uuid, "connection.autoconnect", "yes" if on else "no"])

    def _active_or_none(self) -> List[Dict[str, str]]:
        try:
            return self.active()
        except WifiError:
            return []

    def on_profile(self, device: str, connection_uuid: Optional[str]) -> bool:
        """Whether `device` is on the profile `connection_uuid` now (when nmcli can't say: no, the stricter answer)."""
        try:
            return any(a["device"] == device and a["uuid"] == connection_uuid for a in self.active())
        except WifiError:
            return False

    def _keyfile(self, connection_uuid: str) -> Path:
        return self.connections / f"croom-{connection_uuid}.nmconnection"

    def change(self, plan: Plan, checks: Callable[[str, float], Tuple[List[Check], str]],
               progress: Callable[[str], None] = lambda words: None, **extra: Any) -> str:
        """Join `plan`'s network, then give the device TRIAL_S to pass `checks(device, since)` (which gives the checks and a note for
        the words of success, "" for none); keep it, or go back. The words of what happened; WifiError with them when the device went
        back (or never left). `extra` goes in the record (the touch Pi's job id)."""
        new = plan.reuse or self.new_uuid()
        record = self._save({"state": "trying", "ssid": plan.ssid, "device": plan.device, "old": plan.old, "old_name": plan.old_name,
                             "new": new, "created": plan.reuse is None, "superseded": plan.superseded, "words": f"Joining {plan.ssid}",
                             "reuse_autoconnect": plan.reuse_autoconnect, **extra})
        progress(f"Joining {plan.ssid}")
        if plan.reuse is not None:
            # A saved profile on trial must not connect by itself either: a power cut now must not boot onto an unproven network.
            # What it was is in the record (written above), and is put back if the change is undone.
            code, output = self._set_autoconnect(new, False)
            if code != 0:
                raise WifiError(self.go_back(record, f"Could not prepare {plan.ssid}: {why_words(output)}"))
        else:
            path = self._keyfile(new)
            try:
                write_private(path, keyfile(new, plan.ssid, plan.password, plan.kind, plan.hidden))
            except OSError as e:
                raise WifiError(self.go_back(record, f"Could not save {plan.ssid}'s settings: {e.strerror or e}")) from e
            code, output = self.run(["nmcli", "connection", "load", str(path)])
            if code != 0:
                raise WifiError(self.go_back(record, f"Could not save {plan.ssid}'s settings: {why_words(output)}"))
        code, output = self.run(["nmcli", "--wait", str(CONNECT_WAIT_S), "connection", "up", "uuid", new, "ifname", plan.device])
        if code != 0:
            raise WifiError(self.go_back(record, f"Could not join {plan.ssid}: {why_words(output)}"))
        since = self.clock()
        self._save(record, words=f"Joined {plan.ssid}: checking that the {self.name} can do its job on it")
        progress(f"Joined {plan.ssid}: checking that the {self.name} can do its job on it")
        tests, note = checks(plan.device, since)
        left = f"the {self.name} did not stay on {plan.ssid}"
        failure = self.trial(tests, on_new=lambda: self.on_profile(plan.device, new), left=left)
        if not failure and not self.on_profile(plan.device, new):   # once more, just before it is written down as proven
            failure = left
        if failure:
            raise WifiError(self.go_back(record, f"Joined {plan.ssid}, but {failure}"))
        words = f"Joined {plan.ssid}" + (f". {note}" if note else "")
        self._save(record, state="proven", words=words)
        return self.keep(record)

    def trial(self, checks: List[Check], on_new: Optional[Callable[[], bool]] = None, left: str = "") -> Optional[str]:
        """Ask each check every CHECK_EVERY_S until each has passed once: None, or the failure of the first that had not in TRIAL_S.
        `on_new`, asked before and after each round: the device is still on the new network. When it is not (the network dropped it
        and NetworkManager went back to another), the checks would pass over the other network: the trial fails at once with `left`."""
        deadline = self.monotonic() + TRIAL_S
        passed = set()
        while True:
            if on_new is not None and not on_new():
                return left
            for n, check in enumerate(checks):
                if n in passed:
                    continue
                try:
                    if check.test():
                        passed.add(n)
                except Exception:  # noqa: BLE001 - a check that breaks has not passed
                    pass
            if on_new is not None and not on_new():
                return left
            if len(passed) == len(checks):
                return None
            if self.monotonic() >= deadline:
                return next(c.failure for n, c in enumerate(checks) if n not in passed)
            self.sleep(CHECK_EVERY_S)

    def keep(self, record: Dict[str, Any]) -> str:
        """The new network is proven: it connects by itself from now on, and the profiles it replaces (the same network's) go. Done
        again from the start after a power cut (recover)."""
        new = record.get("new")
        if new:
            code, output = self.run(["nmcli", "connection", "modify", "uuid", new, "connection.autoconnect", "yes"])
            if code != 0:
                record["words"] = f"{record.get('words')}. It may not join it by itself after a restart: {why_words(output)}"
        for old in record.get("superseded") or []:   # the same network's, with the password before (the one it was on, maybe)
            if old != new:
                self.run(["nmcli", "connection", "delete", "uuid", old])
        self._save(record, state="done")
        return record["words"]

    def go_back(self, record: Dict[str, Any], why: str) -> str:
        """Back to the connection the device was on (none: off Wi-Fi again), the new profile deleted when the change made it. The words.
        Every step may be done again: a start that finds the record "undoing" does them all once more."""
        self._save(record, state="undoing", why=why, words=f"{why}: going back")
        old, old_name, new, device = record.get("old"), record.get("old_name") or "the network before", record.get("new"), record.get("device")
        back = None
        if old:
            command = ["nmcli", "--wait", str(CONNECT_WAIT_S), "connection", "up", "uuid", old] + (["ifname", device] if device else [])
            code, output = self.run(command)
            back = None if code == 0 else why_words(output)
        elif new and not record.get("created"):
            self.run(["nmcli", "connection", "down", "uuid", new])
        if new and not record.get("created") and isinstance(record.get("reuse_autoconnect"), bool):
            self._set_autoconnect(new, record["reuse_autoconnect"])   # the saved profile as it was before the change
        if new and record.get("created") and new != old:
            self.run(["nmcli", "connection", "delete", "uuid", new])
            try:
                self._keyfile(new).unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        if not old:
            words = f"{why}. The {self.name} is off Wi-Fi again"
        elif back is None:
            words = f"{why}. The {self.name} is back on {old_name}"
        else:
            words = f"{why}, and going back to {old_name} failed: {back}"
        self._save(record, state="undone", words=words)
        return words

    def recover(self) -> Optional[str]:
        """At the start (after a power cut, a crash): a change that was proven is kept; one that was not is undone. The words of what
        was done, or None when there was nothing to do."""
        record = self.record()
        state = record.get("state")
        if state == "proven":
            # Cut off before it was made to connect by itself: the device came up on the old one. On the new one again, then kept.
            new, device = record.get("new"), record.get("device")
            try:
                on_it = any(a["uuid"] == new for a in self.active())
            except WifiError:
                on_it = False
            if new and not on_it:
                command = ["nmcli", "--wait", str(CONNECT_WAIT_S), "connection", "up", "uuid", new] + (["ifname", device] if device else [])
                code, output = self.run(command)
                if code != 0:
                    return self.go_back(record, f"{record.get('ssid')} could not be joined again after the {self.name} restarted: "
                                                f"{why_words(output)}")
            if not (self.on_profile(device, new) if device else any(a["uuid"] == new for a in self._active_or_none())):
                return self.go_back(record, f"The {self.name} did not stay on {record.get('ssid')} after it restarted")
            return self.keep(record)
        if state in ("trying", "undoing"):
            why = record.get("why") or f"The {self.name} restarted before {record.get('ssid') or 'the new network'} was proven"
            return self.go_back(record, why)
        return None

    def forget(self, ssid: Any) -> str:
        """Delete every saved profile of the network `ssid`. Not the one the device is on when it has no other way to the network (it
        would be cut off)."""
        ssid = check_ssid(ssid)
        if self.under_way():
            raise WifiError("A Wi-Fi change is under way: wait for it to finish")
        profiles = [p for p in self.profiles() if p["ssid"] == ssid]
        if not profiles:
            raise WifiError(f"{ssid} is not saved on the {self.name}")
        devices = self.devices()
        active = {a["uuid"] for a in self.active()}
        if any(p["uuid"] in active for p in profiles) and self.on_ethernet(devices) is None:
            raise WifiError(f"The {self.name} is on {ssid}: join another network first, or it would be cut off")
        for profile in profiles:
            code, output = self.run(["nmcli", "connection", "delete", "uuid", profile["uuid"]])
            if code != 0:
                raise WifiError(f"Could not forget {ssid}: {why_words(output)}")
        self._scan = None
        return f"Forgot {ssid}"

    def gateway_check(self, device: str) -> Check:
        """The device reaches its gateway on `device` (one ping, an argument list)."""
        def test() -> bool:
            gateway = self.gateway(device)
            return gateway is not None and self.run(["ping", "-c", "1", "-W", "2", "-I", device, gateway])[0] == 0
        return Check(test, f"the {self.name} could not reach its gateway within {TRIAL_S} seconds")
