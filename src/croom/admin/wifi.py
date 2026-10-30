"""
The TV Pi's Wi-Fi (spec 2026-10-29, sections 3, 5 and 7): the helper's jobs `wifi_status`, `wifi_scan`, `wifi_connect {ssid, password,
hidden}` and `wifi_forget {ssid}`, through NetworkManager (croom.admin.nmcli, which the touch Pi's agent uses as well).

A change answers at once and is made in the background; wifi_status's "change" follows it. Its safety net: the TV Pi keeps the connection
it was on, joins the new network, and within 90 seconds must reach its gateway there and see the touch Pi check in with the room page
(the helper asks the room page on the loopback, GET /api/devices/seen-since, which answers only the TV Pi itself); otherwise it goes
back and the record says why. Two cases check the gateway alone, and the words say so: a TV Pi on Ethernet (its Wi-Fi is not how the
touch Pi reaches it) and a room no touch Pi has joined. The record is the helper's own (admin-state.json's "wifi": root's alone), written
before each step, so that the helper's next start finishes a change that was proven and undoes one that was not (a power cut, a crash).

While a change is under way the helper refuses a restart, a reboot, an update and another Wi-Fi job; and a change waits for a restart's
watch or an update to end.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from croom.admin.jobs import RESTARTING, UPDATE_UNDER_WAY, WIFI_UNDER_WAY, JobError, Jobs, _only
from croom.admin.nmcli import TRIAL_S, UNDER_WAY, Check, Plan, Wifi, WifiError

logger = logging.getLogger(__name__)

SEEN_SINCE = "/api/devices/seen-since?kind=touch&since={since:.3f}"


class _Store:
    """The change's record in the helper's own record (admin-state.json, "wifi")."""

    def __init__(self, jobs: Jobs):
        self._jobs = jobs

    def load(self) -> Dict[str, Any]:
        record = self._jobs._read_state().get("wifi")
        return record if isinstance(record, dict) else {}

    def save(self, record: Dict[str, Any]) -> None:
        self._jobs._update_state(wifi=record)


class WifiJobs:
    """The Wi-Fi jobs of one helper: they share its Jobs' lock (a change, a restart's watch and an update exclude each other), its
    record and its machine (Host: the runner, the clocks, the background thread, the room page's address)."""

    def __init__(self, jobs: Jobs):
        self._jobs = jobs
        host = jobs._host
        self.wifi = Wifi(run=host.run, store=_Store(jobs), connections=host.paths.nm_connections, name="TV Pi", clock=host.clock,
                         monotonic=host.monotonic, sleep=host.sleep, new_uuid=host.new_uuid)

    # --- one thing at a time

    def _claim(self) -> None:
        with self._jobs._watch_lock:
            if self._jobs._updating:
                raise JobError(UPDATE_UNDER_WAY)
            if self._jobs._watching:
                raise JobError(RESTARTING)
            if self._jobs._wifi_changing:
                raise JobError(WIFI_UNDER_WAY)
            self._jobs._wifi_changing = True

    def _release(self) -> None:
        with self._jobs._watch_lock:
            self._jobs._wifi_changing = False

    # --- the jobs

    def status(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """How the TV Pi is connected (nmcli.Wifi.status) and the last change."""
        _only(args)
        try:
            return self.wifi.status()
        except WifiError as e:
            raise JobError(str(e)) from e

    def scan(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """The networks in range: {"networks": [{"ssid", "signal", "security", "open", "saved", "in_use", ...}], "at"}."""
        _only(args)
        self._claim()
        try:
            return self.wifi.scan(fresh=True)
        except WifiError as e:
            raise JobError(str(e)) from e
        finally:
            self._release()

    def forget(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Forget a saved network (not the one the TV Pi is on, unless it is on Ethernet)."""
        _only(args, "ssid")
        self._claim()
        try:
            words = self.wifi.forget(args.get("ssid"))
        except WifiError as e:
            raise JobError(str(e)) from e
        finally:
            self._release()
        logger.info(words)
        return {"words": words}

    def connect(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Join a network, checked first (a refusal is the answer), then in the background with the safety net: {"changing": true}."""
        _only(args, "ssid", "password", "hidden")
        hidden = args.get("hidden", False)
        if not isinstance(hidden, bool):
            raise JobError("Hidden must be true or false")
        self._claim()
        try:
            plan = self.wifi.prepare(args.get("ssid"), args.get("password", ""), hidden)
            known = plan.ethernet or self._touch_known()
            self._jobs._host.background(lambda: self._change(plan, known))
        except WifiError as e:
            self._release()
            raise JobError(str(e)) from e
        except BaseException:
            self._release()
            raise
        logger.info(f"Joining the Wi-Fi network {plan.ssid}")
        return {"changing": True}

    def _change(self, plan: Plan, touch_known: bool) -> None:
        try:
            words = self.wifi.change(plan, lambda device, since: self._checks(device, since, plan.ethernet, touch_known))
            logger.info(words)
        except WifiError as e:
            logger.warning(f"Wi-Fi: {e}")
        except Exception:  # noqa: BLE001 - whatever broke, the TV Pi must not be left on a network that was not proven
            logger.exception("The Wi-Fi change failed")
            try:
                self.wifi.recover()
            except Exception:  # noqa: BLE001
                logger.exception("Could not go back to the network before")
        finally:
            self._release()

    def resume(self) -> None:
        """At the helper's start: a change the helper's stop cut short is finished (proven) or undone (not), in the background."""
        if self.wifi.record().get("state") not in UNDER_WAY:
            return
        with self._jobs._watch_lock:
            self._jobs._wifi_changing = True
        logger.warning("A Wi-Fi change was under way when the helper stopped: finishing or undoing it")

        def work() -> None:
            try:
                words = self.wifi.recover()
                if words:
                    logger.warning(f"Wi-Fi: {words}")
            except Exception:  # noqa: BLE001
                logger.exception("Could not finish or undo the Wi-Fi change")
            finally:
                self._release()

        self._jobs._host.background(work)

    # --- the TV Pi's checks on a new network

    def _ask(self, since: float) -> Optional[Dict[str, Any]]:
        host, port = self._jobs._control_address()
        return self._jobs._host.ask_room(host, port, SEEN_SINCE.format(since=since))

    def _touch_known(self) -> bool:
        """Whether a touch Pi has joined this room (it is then one of the checks). When the room page does not say, it is assumed to
        have: the stricter check."""
        answer = self._ask(self._jobs._host.clock())
        return not isinstance(answer, dict) or answer.get("known") is not False

    def _checks(self, device: str, since: float, ethernet: bool, touch_known: bool) -> Tuple[List[Check], str]:
        gateway = self.wifi.gateway_check(device)
        if ethernet:
            return [gateway], "The TV Pi is on Ethernet, so only its Wi-Fi gateway was checked"
        if not touch_known:
            return [gateway], "No touch Pi has joined this room, so only the gateway was checked"

        def touch_seen() -> bool:
            answer = self._ask(since)
            return isinstance(answer, dict) and answer.get("seen") is True

        return [gateway, Check(touch_seen, f"the touch Pi did not check in within {TRIAL_S} seconds")], \
            "The TV Pi reached its gateway, and the touch Pi checked in"
