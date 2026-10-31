"""Builds the context model from top Resto Shaman runs (or use "Refresh study" in the web app).

Usage:
    python study.py [--runs 10] [--dungeon "The Blinding Vale"]

Writes data/context_model.json (used by the analyzer) and docs/context_model.md.
Pauses for the hourly API reset when the budget runs low, so it may take hours.
"""
import argparse

from app.config import CACHE_PATH, wcl_credentials
from app.study_runner import DEFAULT_RUNS, run_study
from app.wcl import WCLClient


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    parser.add_argument("--dungeon")
    args = parser.parse_args()
    run_study(WCLClient(*wcl_credentials(), CACHE_PATH), args.runs, args.dungeon)


if __name__ == "__main__":
    main()
