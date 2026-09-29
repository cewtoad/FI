"""Generate the Windows Sandbox config for F1_TR §12 validation.

Writes ``sandbox/F1TR_Sandbox.wsb`` with the CURRENT project paths baked in, so
it always matches this checkout. Requires the Windows Sandbox optional feature
(Containers-DisposableClientVM) to be enabled and a reboot.

Usage:
    py -3.12 sandbox\\make_wsb.py
    # then double-click sandbox\\F1TR_Sandbox.wsb
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
TOOLS = ROOT / "sandbox"
OUT = ROOT / "sandbox_out"

WSB = """<Configuration>
  <MappedFolders>
    <MappedFolder>
      <HostFolder>{dist}</HostFolder>
      <SandboxFolder>Z:\\in</SandboxFolder>
      <ReadOnly>true</ReadOnly>
    </MappedFolder>
    <MappedFolder>
      <HostFolder>{tools}</HostFolder>
      <SandboxFolder>Z:\\tools</SandboxFolder>
      <ReadOnly>true</ReadOnly>
    </MappedFolder>
    <MappedFolder>
      <HostFolder>{out}</HostFolder>
      <SandboxFolder>Z:\\out</SandboxFolder>
      <ReadOnly>false</ReadOnly>
    </MappedFolder>
  </MappedFolders>
  <ClipboardRedirection>true</ClipboardRedirection>
  <LogonCommand>
    <Command>powershell.exe -NoProfile -ExecutionPolicy Bypass -File Z:\\tools\\in_sandbox_test.ps1</Command>
  </LogonCommand>
</Configuration>
"""


def main() -> int:
    if not DIST.is_dir():
        print(f"missing: {DIST} (build the packs first)", file=sys.stderr)
        return 1
    OUT.mkdir(exist_ok=True)
    text = WSB.format(dist=DIST, tools=TOOLS, out=OUT)
    path = TOOLS / "F1TR_Sandbox.wsb"
    path.write_text(text, encoding="utf-8")
    print(f"wrote {path}")
    print(f"  dist  -> Z:\\in    {DIST}")
    print(f"  tools -> Z:\\tools {TOOLS}")
    print(f"  out   -> Z:\\out   {OUT}")
    print("Double-click the .wsb to run (Sandbox feature + reboot required).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
