"""
Creates (or refreshes) %USERPROFILE%\\Desktop\\Ozon Sales.lnk, which opens
the tabbed "Ozon Sales" app (planner_ui.pyw) with the pythonw.exe next to
this interpreter. The shortcut's icon is Ozon's favicon (assets/ozon.ico in
the repo); if that file is missing it falls back to pythonw.exe's icon.
Re-running this on an existing shortcut applies the new icon (Explorer may
show the cached old icon until it refreshes).

Uses the WScript.Shell COM object via `powershell -NoProfile -Command`, so
no extra Python packages are needed. Only after the new shortcut exists are
the legacy Desktop shortcuts (LEGACY_SHORTCUT_NAMES, i.e. the old
"Ozon Daily Planner.lnk") deleted, so the owner always keeps a working icon.
Idempotent: running it again just rewrites the same shortcut and finds
nothing left to remove. Prints the shortcut path.

    python install_desktop_shortcut.py              # app in this script's folder
    python install_desktop_shortcut.py --repo PATH  # app in another checkout

Local only -- touches nothing but the .lnk files.
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

SHORTCUT_NAME = "Ozon Sales.lnk"
LEGACY_SHORTCUT_NAMES = ("Ozon Daily Planner.lnk",)
APP_FILE = "planner_ui.pyw"
ICON_FILE = Path("assets") / "ozon.ico"


def shortcut_path(name=SHORTCUT_NAME):
    return Path(os.environ.get("USERPROFILE") or Path.home()) / "Desktop" / name


def legacy_shortcut_paths():
    return [shortcut_path(n) for n in LEGACY_SHORTCUT_NAMES]


def pythonw_exe():
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return w if w.exists() else exe


def _ps_quote(s):
    return "'" + str(s).replace("'", "''") + "'"


def icon_location(repo, target):
    """IconLocation for the shortcut: the repo's Ozon .ico, else the target exe."""
    ico = Path(repo) / ICON_FILE
    return f"{ico},0" if ico.is_file() else f"{target},0"


def build_ps_command(lnk, target, app, workdir, icon=None):
    return "; ".join([
        "$ErrorActionPreference = 'Stop'",
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut(" + _ps_quote(lnk) + ")",
        "$s.TargetPath = " + _ps_quote(target),
        "$s.Arguments = " + _ps_quote(f'"{app}"'),
        "$s.WorkingDirectory = " + _ps_quote(workdir),
        "$s.IconLocation = " + _ps_quote(icon or f"{target},0"),
        "$s.Description = 'Ozon Sales (M&S -> Ozon sales app: overview, daily tasks, prioritization)'",
        "$s.Save()",
    ])


def main(argv=None):
    ap = argparse.ArgumentParser(description="Create the Ozon Sales Desktop shortcut (and remove the old Ozon Daily Planner one).")
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parent),
                    help="folder that holds planner_ui.pyw (default: this script's folder)")
    args = ap.parse_args(argv)

    repo = Path(args.repo).resolve()
    app = repo / APP_FILE
    if not app.exists():
        print(f"{app} not found.")
        return 1
    lnk = shortcut_path()
    lnk.parent.mkdir(parents=True, exist_ok=True)
    target = pythonw_exe()
    cmd = build_ps_command(lnk, target, app, repo, icon=icon_location(repo, target))
    p = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True)
    if p.returncode != 0 or not lnk.exists():
        print(f"Could not create the shortcut: {(p.stderr or p.stdout).strip()}")
        return 1
    print(lnk)
    for old in legacy_shortcut_paths():
        if old == lnk or not old.exists():
            continue
        try:
            old.unlink()
            print(f"Removed old shortcut {old}")
        except OSError as e:
            print(f"Warning: could not remove old shortcut {old}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
