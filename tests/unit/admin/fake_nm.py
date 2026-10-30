"""
A stand-in for NetworkManager as nmcli shows it, for the TV Pi's Wi-Fi jobs (croom.admin.wifi) and the touch Pi's agent: a Wi-Fi device
(and an Ethernet one, plugged in or not), the saved profiles, the one each device is on, the networks in range and the passwords that work,
whether a network's gateway answers a ping. Its outputs are nmcli's terse ones, with ":" and "\\" escaped as nmcli escapes them. A keyfile
loaded with `nmcli connection load` is read back from the disk (it must be mode 600), as NetworkManager reads it. Nothing touches the
machine's network.
"""

import os
import stat
from pathlib import Path

WRONG_PASSWORD = "Error: Connection activation failed: Secrets were required, but not provided."


class PowerCut(BaseException):
    """The power goes: nothing after it happens (not an Exception: no `except Exception` carries on after it)."""


def esc(value):
    return str(value).replace("\\", "\\\\").replace(":", "\\:")


def unkf(value):
    """A GLib key file's string value, read."""
    out, i = [], 0
    while i < len(value):
        if value[i] == "\\" and i + 1 < len(value):
            out.append({"s": " ", "\\": "\\", "n": "\n", "t": "\t"}.get(value[i + 1], value[i + 1]))
            i += 2
        else:
            out.append(value[i])
            i += 1
    return "".join(out)


def read_keyfile(path):
    sections, current = {}, None
    for line in Path(path).read_text().splitlines():
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], {})
        elif "=" in line and current is not None:
            key, _, value = line.partition("=")
            current[key] = value
    return sections


class FakeNM:
    def __init__(self, connections_dir=None):
        self.dir = Path(connections_dir) if connections_dir else None
        self.wifi_device = "wlan0"          # None: a device without Wi-Fi
        self.ethernet = False               # an Ethernet cable plugged in and connected
        self.profiles = {"u-old": {"name": "Office", "ssid": "Office", "psk": "office-password", "autoconnect": True, "file": None,
                                   "hidden": False, "mgmt": "wpa-psk"}}
        self.active = {"wlan0": "u-old"}
        self.air = {"Office": (70, "WPA2"), "Guest": (50, ""), "New Net": (80, "WPA1 WPA2"), "Corp": (60, "WPA2 802.1X"),
                    "Old Cafe": (40, "WEP"), "Modern": (65, "WPA3")}
        self.passwords = {"Office": "office-password", "New Net": "correct horse", "Modern": "sae password"}
        self.gateways = {"Office": "192.168.1.1", "Guest": "10.9.0.1", "New Net": "172.16.0.1", "Modern": "192.168.7.1", "Hidden One": "10.1.1.1"}
        self.reachable = lambda ssid: True  # whether the gateway of the network the Wi-Fi is on answers a ping
        self.scan_fails = False
        self.rescan_refused = False
        self.calls = []                     # every command, without the env LC_ALL=C LANG=C before it
        self.without_c_locale = []          # commands that came without it (none should)
        self.cut = None                     # run before each command: raises PowerCut at the chosen one
        self.after = None                   # run after each command, with it: a test changes the air (a network drops the device)
        self.used = {"u-old": 1}            # when each profile was last connected (NetworkManager's timestamp): a counter

    # --- what the device is on

    def ssid_on(self, device=None):
        uuid = self.active.get(device or self.wifi_device)
        return self.profiles[uuid]["ssid"] if uuid in self.profiles else None

    def reboot(self):
        """A power cut and the boot after it: the Wi-Fi comes up as NetworkManager brings it up, on the most recently used profile
        that connects by itself and whose network is in range and takes its password (the old one, since a profile on trial does not
        connect by itself)."""
        self.active = {}
        ready = [uuid for uuid, profile in self.profiles.items() if profile["autoconnect"] and self._works(profile)]
        if ready:
            self.active[self.wifi_device] = max(ready, key=lambda uuid: self.used.get(uuid, 0))

    def _works(self, profile):
        ssid = profile["ssid"]
        if ssid not in self.air and not profile.get("hidden"):
            return False
        if ssid in self.passwords and profile.get("psk") != self.passwords[ssid]:
            return False
        return ssid in self.air or ssid in self.gateways

    # --- nmcli

    def __call__(self, args, env=None, timeout=None):
        args = list(args)
        if args[:3] == ["env", "LC_ALL=C", "LANG=C"]:
            args = args[3:]
        else:
            self.without_c_locale.append(args)
        if self.cut:
            self.cut(args)
        self.calls.append(list(args))
        answer = self._answer(args)
        if self.after:
            self.after(args)
        return answer

    def _answer(self, args):
        if args[0] == "ping":
            ssid = self.ssid_on(args[args.index("-I") + 1])
            ok = ssid is not None and self.gateways.get(ssid) == args[-1] and self.reachable(ssid)
            return (0, "1 packets transmitted, 1 received") if ok else (1, "1 packets transmitted, 0 received")
        assert args[0] == "nmcli", args
        rest = list(args[1:])
        if rest[:2] == ["--wait", "45"]:
            rest = rest[2:]
        if rest[:2] == ["-t", "-f"]:
            return self._terse(rest[2], rest[3:])
        if rest[:2] == ["connection", "load"]:
            return self._load(rest[2])
        if rest[:3] == ["connection", "up", "uuid"]:
            return self._up(rest[3], rest[rest.index("ifname") + 1] if "ifname" in rest else self.wifi_device)
        if rest[:3] == ["connection", "modify", "uuid"]:
            uuid = rest[3]
            if uuid not in self.profiles:
                return 10, "Error: unknown connection"
            assert rest[4] == "connection.autoconnect" and rest[5] in ("yes", "no"), rest
            self.profiles[uuid]["autoconnect"] = rest[5] == "yes"
            return 0, ""
        if rest[:3] == ["connection", "delete", "uuid"]:
            uuid = rest[3]
            profile = self.profiles.pop(uuid, None)
            if profile is None:
                return 10, f"Error: unknown connection '{uuid}'."
            for device, on in list(self.active.items()):
                if on == uuid:
                    del self.active[device]
            if profile.get("file"):
                Path(profile["file"]).unlink(missing_ok=True)
            return 0, f"Connection '{profile['name']}' ({uuid}) successfully deleted."
        if rest[:3] == ["connection", "down", "uuid"]:
            for device, on in list(self.active.items()):
                if on == rest[3]:
                    del self.active[device]
            return 0, ""
        raise AssertionError(f"unexpected nmcli {args}")

    def _terse(self, fields, rest):
        if fields == "DEVICE,TYPE,STATE,CONNECTION" and rest == ["device", "status"]:
            lines = []
            if self.ethernet:
                lines.append("eth0:ethernet:connected:Wired connection 1")
            else:
                lines.append("eth0:ethernet:unavailable:")
            if self.wifi_device:
                uuid = self.active.get(self.wifi_device)
                name = self.profiles[uuid]["name"] if uuid in self.profiles else ""
                lines.append(f"{self.wifi_device}:wifi:{'connected' if name else 'disconnected'}:{esc(name)}")
                lines.append(f"p2p-dev-{self.wifi_device}:wifi-p2p:disconnected:")
            lines.append("lo:loopback:connected (externally):lo")
            return 0, "\n".join(lines) + "\n"
        if fields == "NAME,UUID,TYPE,DEVICE" and rest == ["connection", "show", "--active"]:
            lines = [f"{esc(self.profiles[u]['name'])}:{u}:802-11-wireless:{d}" for d, u in self.active.items() if u in self.profiles]
            if self.ethernet:
                lines.append("Wired connection 1:u-eth:802-3-ethernet:eth0")
            return 0, "\n".join(lines) + "\n"
        if fields == "NAME,UUID,TYPE" and rest == ["connection", "show"]:
            lines = [f"{esc(p['name'])}:{u}:802-11-wireless" for u, p in self.profiles.items()]
            lines.append("Wired connection 1:u-eth:802-3-ethernet")
            return 0, "\n".join(lines) + "\n"
        if fields == "connection.autoconnect" and rest[:3] == ["connection", "show", "uuid"]:
            profile = self.profiles.get(rest[3])
            return (0, f"connection.autoconnect:{'yes' if profile['autoconnect'] else 'no'}\n") if profile else (10, "Error: no such profile.")
        if fields == "802-11-wireless.ssid" and rest[:3] == ["connection", "show", "uuid"]:
            profile = self.profiles.get(rest[3])
            return (0, f"802-11-wireless.ssid:{esc(profile['ssid'])}\n") if profile else (10, "Error: no such connection profile.")
        if fields in ("IP4.ADDRESS", "IP4.GATEWAY") and rest[:2] == ["device", "show"]:
            device = rest[2]
            if device == "eth0":
                return 0, ("IP4.ADDRESS[1]:192.168.50.20/24\n" if fields == "IP4.ADDRESS" else "IP4.GATEWAY:192.168.50.1\n")
            ssid = self.ssid_on(device)
            if ssid is None:
                return 0, f"{fields}:\n" if fields == "IP4.GATEWAY" else ""
            gateway = self.gateways.get(ssid, "")
            if fields == "IP4.GATEWAY":
                return 0, f"IP4.GATEWAY:{gateway}\n"
            return 0, f"IP4.ADDRESS[1]:{gateway.rsplit('.', 1)[0]}.42/24\n"
        if fields == "IN-USE,SSID,SIGNAL,SECURITY" and rest[:3] == ["device", "wifi", "list"]:
            rescan = rest[rest.index("--rescan") + 1]
            if self.scan_fails:
                return 10, "Error: Wi-Fi is disabled."
            if rescan == "yes" and self.rescan_refused:
                return 1, "Error: Scanning not allowed immediately following previous scan."
            here = self.ssid_on(rest[rest.index("ifname") + 1])
            lines = [f"{'*' if ssid == here else ' '}:{esc(ssid)}:{signal}:{esc(security)}" for ssid, (signal, security) in self.air.items()]
            lines.append(" ::30:WPA2")    # a network that hides its name
            return 0, "\n".join(lines) + "\n"
        raise AssertionError(f"unexpected nmcli -t -f {fields} {rest}")

    def _load(self, path):
        info = os.stat(path)
        assert stat.S_IMODE(info.st_mode) == 0o600, "a keyfile with a password must be root's alone"
        sections = read_keyfile(path)
        connection, wifi = sections["connection"], sections["wifi"]
        ssid = bytes(int(b) for b in wifi["ssid"].split(";") if b).decode("utf-8")
        security = sections.get("wifi-security", {})
        self.profiles[connection["uuid"]] = {
            "name": unkf(connection["id"]), "ssid": ssid, "psk": unkf(security["psk"]) if "psk" in security else None,
            "autoconnect": connection.get("autoconnect") != "false", "file": str(path), "hidden": wifi.get("hidden") == "true",
            "mgmt": security.get("key-mgmt")}
        return 0, ""

    def _up(self, uuid, device):
        profile = self.profiles.get(uuid)
        if profile is None:
            return 10, f"Error: unknown connection '{uuid}'."
        ssid = profile["ssid"]
        if ssid not in self.air and not (profile.get("hidden") and ssid in self.gateways):
            self.active.pop(device, None)
            return 4, f"Error: Connection activation failed: No network with SSID '{ssid}' found."
        if ssid in self.passwords and profile.get("psk") != self.passwords[ssid]:
            self.active.pop(device, None)
            return 4, WRONG_PASSWORD
        self.active[device] = uuid
        self.used[uuid] = max(self.used.values(), default=0) + 1
        return 0, f"Connection successfully activated (D-Bus active path: /org/freedesktop/NetworkManager/ActiveConnection/{len(self.calls)})"
