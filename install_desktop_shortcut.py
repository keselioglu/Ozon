"""
Creates (or refreshes) %USERPROFILE%\\Desktop\\Ozon Daily Planner.lnk, which
opens planner_ui.pyw with the pythonw.exe next to this interpreter.

Uses the WScript.Shell COM object via `powershell -NoProfile -Command`, so
no extra Python packages are needed. Idempotent: running it again just
rewrites the same shortcut. Prints the shortcut path.

    python install_desktop_shortcut.py              # app in this script's folder
    python install_desktop_shortcut.py --repo PATH  # app in another checkout

Local only -- touches nothing but the .lnk file.
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

SHORTCUT_NAME = "Ozon Daily Planner.lnk"
APP_FILE = "planner_ui.pyw"


def shortcut_path():
    return Path(os.environ.get("USERPROFILE") or Path.home()) / "Desktop" / SHORTCUT_NAME


def pythonw_exe():
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return w if w.exists() else exe


def _ps_quote(s):
    return "'" + str(s).replace("'", "''") + "'"


def build_ps_command(lnk, target, app, workdir):
    return "; ".join([
        "$ErrorActionPreference = 'Stop'",
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut(" + _ps_quote(lnk) + ")",
        "$s.TargetPath = " + _ps_quote(target),
        "$s.Arguments = " + _ps_quote(f'"{app}"'),
        "$s.WorkingDirectory = " + _ps_quote(workdir),
        "$s.IconLocation = " + _ps_quote(f"{target},0"),
        "$s.Description = 'Ozon Daily Planner (M&S -> Ozon daily new-offer plan)'",
        "$s.Save()",
    ])


def main(argv=None):
    ap = argparse.ArgumentParser(description="Create the Ozon Daily Planner Desktop shortcut.")
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
    cmd = build_ps_command(lnk, pythonw_exe(), app, repo)
    p = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True)
    if p.returncode != 0 or not lnk.exists():
        print(f"Could not create the shortcut: {(p.stderr or p.stdout).strip()}")
        return 1
    print(lnk)
    return 0


if __name__ == "__main__":
    sys.exit(main())
