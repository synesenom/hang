"""Start the background listening at login (`./hang` turns it on, `./hang off` off).

A LaunchAgent can't use the microphone itself without its own permission, so
at login it only opens a Terminal window running `./hang start`: the listening
then runs from Terminal, which already has microphone access, and the window
closes itself after a few seconds.
"""
import os
import plistlib
import shlex
import subprocess

from hang import ROOT

LABEL = "com.hang.autostart"
PLIST = os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LABEL)
SCRIPT = os.path.join(ROOT, "models", "hang-login.command")


def enabled():
    return os.path.exists(PLIST)


def enable():
    os.makedirs(os.path.dirname(SCRIPT), exist_ok=True)
    with open(SCRIPT, "w") as f:
        # The window closes itself once this script has exited (closing it
        # earlier would make Terminal ask whether to stop the script).
        f.write("#!/bin/zsh\n"
                "export PATH=%s\n"
                "me=$(tty)\n"
                "%s start\n"
                "sleep 4\n"
                "( sleep 1; osascript -e 'tell application \"Terminal\" to close "
                "(every window whose tty of tab 1 is \"'$me'\")' ) >/dev/null 2>&1 &!\n"
                % (shlex.quote(os.environ.get("PATH", "")), shlex.quote(os.path.join(ROOT, "hang"))))
    os.chmod(SCRIPT, 0o700)
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    with open(PLIST, "wb") as f:
        plistlib.dump({"Label": LABEL, "ProgramArguments": ["/usr/bin/open", "-a", "Terminal", SCRIPT],
                       "RunAtLoad": True}, f)
    # launchd picks it up at the next login (loading it now would open a window right away).


def disable():
    if not enabled():
        return False
    subprocess.run(["launchctl", "bootout", "gui/%d" % os.getuid(), PLIST], capture_output=True)
    os.unlink(PLIST)
    try:
        os.unlink(SCRIPT)
    except OSError:
        pass
    return True
