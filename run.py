"""
Launcher — seeds the database if needed, then starts the FastAPI server.

Usage:
  python run.py
  python run.py --host 0.0.0.0 --port 8080
"""

import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DB_PATH = Path(os.getenv("DB_PATH", "data/sample.db"))


def ensure_database() -> None:
    if DB_PATH.exists():
        return
    print(f"Database not found at {DB_PATH} — seeding now...")
    result = subprocess.run(
        [sys.executable, "data/seed_db.py"],
        check=False,
    )
    if result.returncode != 0:
        print("ERROR: database seeding failed.")
        sys.exit(1)
    print("Database ready.\n")


if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="Autonomous Data Analyst server")
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", 8000)))
    args = parser.parse_args()

    ensure_database()

    print(f"Starting server at http://{args.host}:{args.port}")
    uvicorn.run("app:app", host=args.host, port=args.port, reload=False)
