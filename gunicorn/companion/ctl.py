#
# This file is part of gunicorn released under the MIT license.
# See the NOTICE for more information.

"""Command-line client for the companion control socket.

Speaks the newline-delimited JSON protocol the manager's ControlServer serves:
sends one command and prints the manager's reply. Installed as the
``gunicorn-companion`` console script.
"""

import argparse
import json
import os
import socket
import sys

# Commands that act on one named companion and so require a name argument.
PER_NAME_COMMANDS = ("start", "stop", "restart")
COMMANDS = ("status", "reread") + PER_NAME_COMMANDS


def send_command(socket_path, command):
    """Send one command dict to the control socket and return the reply dict."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.connect(socket_path)
        client.sendall((json.dumps(command) + "\n").encode("utf-8"))
        chunks = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
    finally:
        client.close()
    return json.loads(b"".join(chunks).decode("utf-8"))


def build_parser():
    parser = argparse.ArgumentParser(
        prog="gunicorn-companion",
        description="Control gunicorn companion processes.")
    parser.add_argument(
        "-s", "--socket",
        default=os.environ.get("GUNICORN_COMPANION_SOCKET"),
        help="path to the companion control socket "
             "(defaults to $GUNICORN_COMPANION_SOCKET)")
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument(
        "name", nargs="?",
        help="companion name (required for start, stop, restart)")
    return parser


def run(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.socket:
        parser.error("no control socket; pass --socket or set "
                     "GUNICORN_COMPANION_SOCKET")
    if args.command in PER_NAME_COMMANDS and not args.name:
        parser.error("%s requires a companion name" % args.command)

    command = {"cmd": args.command}
    if args.name:
        command["name"] = args.name

    try:
        response = send_command(args.socket, command)
    except OSError as error:
        print("cannot reach companion socket %s: %s" % (args.socket, error),
              file=sys.stderr)
        return 2

    print(json.dumps(response, indent=2))
    return 0 if response.get("ok") else 1


if __name__ == "__main__":
    sys.exit(run())
