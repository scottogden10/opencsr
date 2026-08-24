#!/usr/bin/env python3
"""Launch the OpenCSR review workbench.

    python3 serve.py                      # offline mock backend
    python3 serve.py --backend managed    # Claude Managed Agents (API key needed)
"""

import argparse

from opencsr.server import serve

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="mock", choices=["mock", "managed"])
    ap.add_argument("--db", default="opencsr.db")
    ap.add_argument("--port", type=int, default=8734)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    serve(db_path=args.db, backend=args.backend, host=args.host, port=args.port)
