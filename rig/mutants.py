#!/usr/bin/env python3
"""Whether the rig can fail: break a copy of presenced, and require the named check to catch it.

A check that has never failed proves nothing, and this rig has had some. Each row
below breaks one thing in a copy of the code; the run passes only if the desktop
check goes red on the check named there.
"""
import concurrent.futures
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent

# name, file, the text to replace, its replacement, desktop, check, what must fail
MUTANTS = [
    ("no hold", "presenced/monitor.py", "            and sure is not None\n", "            and False\n",
     "gnome", "leave", "a face takes the hold"),
    ("a release reads as input", "presenced/service.py", "            return last_event <= silent_since\n",
     "            return True\n", "gnome", "leave", "the idle reset of a release is not a return"),
    ("away locks nothing", "rig/scenario.py", 'config.actions.on_away = "loginctl lock-session $PRESENCED_SESSION"',
     "pass", "gnome", "leave", "away locks through on_away"),
    ("a video's end reads as input", "presenced/service.py", "            return last_event <= silent_since\n",
     "            return True\n", "gnome", "stay", "a video ending is not read as input"),
    ("forced looks while locked", "presenced/service.py", "        self.monitor.force_looks = not locked\n", "",
     "gnome", "stay", "no forced looks while locked"),
    ("our hold reads as a video", "presenced/desktop.py", "and app[0] != APP:", "and True:",
     "gnome", "contract", "our own inhibitor reads as another program's"),
    ("no release", "presenced/keep.py", 'self.ask(target, "UnInhibit", "u", (cookie,))',
     'self.ask(target, "Ping", "", ())', "gnome", "contract", "the inhibitor survived its release"),
    ("KDE videos unseen", "presenced/desktop.py", "return any(app != APP for app, _ in held[0])", "return False",
     "kde", "stay", "a video is noticed"),
    ("no Xfce suspend hold", "presenced/keep.py", "    (XFCE_SAVER, POWER),\n", "    (XFCE_SAVER,),\n",
     "xfce", "contract", "holds is 1, not 2"),
    ("detector blind", "presenced/ir.py", "verdict = Verdict.PRESENT if seen.faces else Verdict.ABSENT",
     "verdict = Verdict.ABSENT", "gnome", "sensors", "the detector finds the face"),
    ("sonar deaf", "presenced/sonar.py",
     "verdict = Verdict.PRESENT if reading.motion_db > self.threshold_db else Verdict.QUIET",
     "verdict = Verdict.QUIET", "gnome", "sensors", "the sonar hears motion"),
    ("no metering", "presenced/ir.py", "if n == 0 and self.metering:", "if False:",
     "gnome", "sensors", "the detector finds the face"),
    ("no waiting for the exposure", "presenced/ir.py", "if self.clipped(brighter) < CLIPPED:", "if True:",
     "gnome", "sensors", "the detector finds the face"),
    ("idle action not armed", "rig/sessions/gnome.sh", 'sleep-inactive-$power-type" logout', 'sleep-inactive-$power-type" nothing',
     "gnome", "idle", "gsd-power's idle action is logging out"),
]


def tried(mutant):
    name, path, old, new, desktop, check, caught_by = mutant
    with tempfile.TemporaryDirectory() as temporary:
        copy = pathlib.Path(temporary) / "src"
        shutil.copytree(REPO, copy, ignore=shutil.ignore_patterns(".git", ".venv", "build", "__pycache__"))
        target = copy / path
        text = target.read_text()
        if text.count(old) != 1:
            return f"{name}: no longer applies to {path}"
        target.write_text(text.replace(old, new))
        run = subprocess.run(
            [REPO / "rig/run.sh", desktop, check], capture_output=True, text=True,
            env={**os.environ, "PRESENCED_SRC": str(copy)},
        )
    if run.returncode == 0:
        return f"{name}: SURVIVED {desktop} {check}"
    if caught_by not in run.stdout:
        return f"{name}: failed, but not on {caught_by!r}"
    return None


with concurrent.futures.ThreadPoolExecutor(int(os.environ.get("JOBS", "4"))) as pool:
    chosen = [mutant for mutant in MUTANTS if not sys.argv[1:] or mutant[0] in sys.argv[1:]]
    wrong = [result for result in pool.map(tried, chosen) if result]
print("\n".join(wrong) or f"all {len(chosen)} caught")
sys.exit(1 if wrong else 0)
