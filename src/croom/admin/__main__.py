"""
`python -m croom.admin`: the root helper, as croom-admin.service runs it.

    python -m croom.admin --owner pi --group pi
    python -m croom.admin --check

`--check` imports everything the helper runs and makes its jobs, without listening or touching the machine, and exits 0: an update
runs it with a new release's Python before switching to it, so that a release whose helper would not start is never switched to.

`--owner` and `--group` are the user the room software runs as and its group: the config and keys the helper writes are theirs, and
the socket's group is theirs, so that the room software (and only it, and root) can ask.
"""

import argparse
import asyncio
import logging
import signal

from croom.admin.client import SOCKET_PATH
from croom.admin.jobs import Host, Jobs, Paths
from croom.admin.server import AdminServer


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="python -m croom.admin", description="Crystal Meet root helper (croom-admin)")
    parser.add_argument("--socket", default=SOCKET_PATH, help=f"the socket to listen on (default {SOCKET_PATH})")
    parser.add_argument("--owner", default="croom", help="the user the room software runs as (default croom)")
    parser.add_argument("--group", default=None, help="the room software's group: the socket's and its files' (default the owner's name)")
    parser.add_argument("--check", action="store_true", help="make the jobs and exit 0, without listening: this release's helper starts")
    args = parser.parse_args(argv)
    group = args.group or args.owner
    if args.check:
        AdminServer(args.socket, Jobs(Host(paths=Paths(), owner=args.owner, group=group)).table, group=group)
        print("The helper starts")
        return

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    jobs = Jobs(Host(paths=Paths(), owner=args.owner, group=group))
    jobs.resume()
    try:
        asyncio.run(_serve(AdminServer(args.socket, jobs.table, group=group)))
    except KeyboardInterrupt:
        pass


async def _serve(server: AdminServer) -> None:
    """Serve until systemd stops the helper (SIGTERM), then remove the socket: a socket left behind would look like a helper there."""
    task = asyncio.current_task()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        await server.serve()
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    main()
