"""Builds the context model from top runs of one healer spec (or use "Refresh study" in the web app).

Usage:
    python study.py [--spec resto_shaman] [--runs 10] [--dungeon "The Blinding Vale"]

Writes data/context_models/<spec>.json (used by the analyzer) and docs/context_models/<spec>.md.
Pauses for the hourly API reset when the budget runs low, so it may take hours.
"""
import argparse

from app.config import CACHE_PATH, wcl_credentials
from app.specs import SPECS
from app.study_runner import DEFAULT_RUNS, run_study
from app.wcl import WCLClient


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", default=SPECS[0].key, choices=[s.key for s in SPECS])
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    parser.add_argument("--dungeon")
    args = parser.parse_args()
    spec = next(s for s in SPECS if s.key == args.spec)
    run_study(WCLClient(*wcl_credentials(), CACHE_PATH), spec, args.runs, args.dungeon)


if __name__ == "__main__":
    main()
