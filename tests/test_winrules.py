"""The Windows firewall rules cover every program that listens (runs anywhere: it only builds the script).

    python tests/test_winrules.py
"""
import sys
from pathlib import Path, PureWindowsPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from airlog import winservice  # noqa: E402

script = winservice.firewall_script(PureWindowsPath(r"C:\Program Files\Airlog"))
for prog in ("Airlog.exe", "ffmpeg.exe"):               # ffmpeg.exe listens for SRT / UDP / RTP
    for proto in ("TCP", "UDP"):
        assert f"-Program 'C:\\Program Files\\Airlog\\{prog}' -Protocol {proto} -Profile Private,Domain" in script, \
            (prog, proto, script)
assert script.startswith("Get-NetFirewallRule -DisplayName 'Airlog Recorder'") and "Remove-NetFirewallRule" in script
assert script.count("New-NetFirewallRule") == 4 and "Public" not in script
print("winrules OK")
