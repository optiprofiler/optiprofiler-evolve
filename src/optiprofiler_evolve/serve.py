"""Serve one run's dashboard over HTTP for a browser, nothing more.

``python -m optiprofiler_evolve.serve <run_dir>`` serves the PRIVATE owner
console; ``--public`` serves only the sanitized ``run_dir/public`` bundle.
The server binds to loopback unless ``--host`` is passed explicitly, never
opens a browser, and serves only the selected directory — never the parent
that may hold other runs.
"""

from __future__ import annotations

import argparse
import functools
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class DashboardError(RuntimeError):
    """A run directory that cannot be served as requested."""


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return


def prepare(
    run_dir: Path,
    *,
    public: bool = False,
    host: str = "127.0.0.1",
    port: int = 0,
) -> tuple[ThreadingHTTPServer, str, list[str]]:
    """Validate the run, bind the server, and return (server, url, warnings)."""

    run_dir = run_dir.resolve()
    if not run_dir.is_dir():
        raise DashboardError(f"Run directory does not exist: {run_dir}")
    root = run_dir / "public" if public else run_dir
    page = root / "status.html"
    if not page.is_file():
        if public:
            raise DashboardError(
                f"No sanitized dashboard at {page}. The public bundle is "
                "materialized while an evolve run executes; check that this is "
                "a run directory and that the run has started."
            )
        raise DashboardError(
            f"No owner dashboard at {page}. Point the command at one evolve "
            "run directory (the folder holding events.jsonl and status.html)."
        )

    handler = functools.partial(_QuietHandler, directory=str(root))
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    bound_host, bound_port = server.server_address[:2]
    display_host = "127.0.0.1" if str(bound_host) in {"0.0.0.0", "::", ""} else str(bound_host)
    url = f"http://{display_host}:{bound_port}/status.html"

    warnings = []
    if str(bound_host) not in {"127.0.0.1", "::1"}:
        exposure = "the sanitized public bundle" if public else "PRIVATE owner evidence"
        warnings.append(
            f"WARNING: listening on {bound_host} exposes {exposure} to the "
            "network. The owner console must never be shared; use --public "
            "for collaborators."
        )
    return server, url, warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path, help="One evolve run directory")
    parser.add_argument(
        "--public",
        action="store_true",
        help="Serve only the sanitized run_dir/public bundle",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address (default 127.0.0.1; pass 0.0.0.0 explicitly "
        "to allow external access)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Port to bind (default 0 picks a free port and prints it)",
    )
    args = parser.parse_args(argv)
    try:
        server, url, warnings = prepare(
            args.run_dir, public=args.public, host=args.host, port=args.port
        )
    except DashboardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for warning in warnings:
        print(warning, file=sys.stderr)
    label = "public dashboard" if args.public else "PRIVATE owner console"
    print(f"Serving {label} for {args.run_dir.resolve()}", flush=True)
    print(f"Open: {url}", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
