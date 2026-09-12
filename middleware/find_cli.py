"""
find_cli.py — print the path of the Claude CLI this installation will use.

Exists as a FILE, not as a `python -c "..."` snippet in the launcher.

The launcher originally embedded this logic inline. PowerShell rebuilds the command
line when it invokes a native executable, and the double quotes inside the snippet
did not survive that, so python.exe received malformed source and failed with a bare
`File "<string>", line 10`. Multi-line inline scripts with quotes are simply not
safe to pass this way. A real file has no quoting to get wrong.

Resolution order matches the SDK's own _find_cli(): the copy bundled inside
claude-agent-sdk first, then PATH. The bundled binary is why an installed ProjectZen
needs no Node, no npm and no separate CLI install.

Prints the path and exits 0 when found; prints nothing and exits 1 when not.
"""

import pathlib
import shutil
import sys


def find() -> str:
    try:
        import claude_agent_sdk
        bundled = pathlib.Path(claude_agent_sdk.__file__).parent / "_bundled"
        for name in ("claude.exe", "claude"):
            candidate = bundled / name
            if candidate.exists():
                return str(candidate)
    except Exception:
        pass
    return shutil.which("claude") or ""


if __name__ == "__main__":
    path = find()
    if not path:
        sys.exit(1)
    sys.stdout.write(path)
    sys.exit(0)
