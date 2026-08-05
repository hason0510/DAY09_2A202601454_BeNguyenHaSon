"""Run configuration.

The model name lives here (not in .env) on purpose: README section 9 requires the
model to be declared in source code so it can be graded, while only the secret
API key belongs in .env.
"""

from __future__ import annotations

import os
from pathlib import Path

# --------------------------------------------------------------------------
# Model declaration (graded artifact -- keep in sync with logging/metadata.json)
# --------------------------------------------------------------------------
MODEL_NAME = "gpt-4o-mini"

# OpenAI does not publish a parameter count for gpt-4o-mini. We record the fact
# rather than inventing a number that would look compliant but be unverifiable.
MODEL_PARAM_SIZE = "not publicly disclosed by OpenAI (widely estimated ~8B)"
MODEL_PROVIDER = "openai"

# Deterministic decoding: every LLM hop is a classification or review check,
# never a creative one, so we pin temperature and seed to make reruns stable.
TEMPERATURE = 0.0
SEED = 7
MAX_OUTPUT_TOKENS = 700
REQUEST_TIMEOUT_S = 60
MAX_RETRIES = 3

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
INPUT_DIR = ROOT / "input"
OUTPUT_DIR = ROOT / "output"
LOG_DIR = ROOT / "logging"
TRACE_PATH = LOG_DIR / "trace.jsonl"
METADATA_PATH = LOG_DIR / "metadata.json"

POLICY_VERSION = "EC_POLICY_V2"

# How many cases run concurrently. Each case is an independent A2A conversation,
# so parallelism does not change any output -- only wall clock.
MAX_WORKERS = 6

# Verifier repair loop: how many times a failing draft is handed back before we
# accept the deterministic fallback and flag the case in the run report.
MAX_REPAIR_ROUNDS = 2


def api_key() -> str | None:
    return os.environ.get("OPENAI_API_KEY")


def base_url() -> str | None:
    return os.environ.get("OPENAI_BASE_URL")
