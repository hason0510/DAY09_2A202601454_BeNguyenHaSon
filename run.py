"""Entry point: run the 50-case investigation.

    python run.py                 # full run (uses the LLM agents)
    python run.py --no-llm        # deterministic core only, zero API cost
    python run.py --cases EC_001 EC_012
    python run.py --workers 1     # serialise, e.g. for a readable trace

Writes: output/EC_xxx.json, logging/trace.jsonl (truncated per run),
logging/metadata.json.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src import config  # noqa: E402
from src.a2a.bus import Bus  # noqa: E402
from src.a2a.trace import TraceWriter  # noqa: E402
from src.agents.fact_agents import (  # noqa: E402
    CustomerAgent,
    DeliveryAgent,
    OrderProductAgent,
    PaymentAgent,
)
from src.agents.reasoning_agents import CriticAgent, IntakeAgent, PolicyAgent  # noqa: E402
from src.agents.verifier_agent import VerifierAgent  # noqa: E402
from src.llm.client import LLMClient  # noqa: E402
from src.pipeline import Coordinator  # noqa: E402
from src.store.olist_store import load_store  # noqa: E402


def load_env(root: Path) -> None:
    """Read .env without requiring python-dotenv to be installed."""
    try:
        from dotenv import load_dotenv

        load_dotenv(root / ".env")
        return
    except ImportError:
        pass
    env_path = root / ".env"
    if not env_path.exists():
        return
    import os

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-llm", action="store_true", help="skip every LLM hop")
    parser.add_argument("--cases", nargs="*", help="case ids to run (default: all)")
    parser.add_argument("--workers", type=int, default=config.MAX_WORKERS)
    parser.add_argument("--out", help="output directory (default: output/)")
    parser.add_argument("--category-language", choices=("pt", "en"))
    parser.add_argument("--ranked-causes", choices=("single", "contributing"))
    parser.add_argument("--evidence-sellers", choices=("responsible", "all"))
    args = parser.parse_args()

    # Interpretation switches: CLI overrides the defaults in config.py so a
    # variant can be generated side by side without editing source.
    if args.out:
        config.OUTPUT_DIR = Path(args.out).resolve()
    if args.category_language:
        config.CATEGORY_LANGUAGE = args.category_language
    if args.ranked_causes:
        config.RANKED_CAUSES = args.ranked_causes
    if args.evidence_sellers:
        config.EVIDENCE_SELLERS = args.evidence_sellers

    load_env(config.ROOT)
    run_id = f"run_{time.strftime('%Y%m%d_%H%M%S')}"
    started = time.perf_counter()

    cases = []
    for path in sorted(config.INPUT_DIR.glob("EC_*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        if args.cases and case["case_id"] not in args.cases:
            continue
        cases.append(case)
    if not cases:
        print("no input cases matched", file=sys.stderr)
        return 1

    print(f"[{run_id}] loading Olist CSVs ...")
    store = load_store(config.DATA_DIR)
    print(f"  {store.stats}")

    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    tracer = TraceWriter(config.TRACE_PATH, run_id)

    llm = LLMClient(tracer=tracer, enabled=not args.no_llm)
    if not llm.enabled and not args.no_llm:
        print("  ! OPENAI_API_KEY missing or openai sdk absent -> running deterministic core only")

    bus = Bus(store, tracer)
    for agent in (
        IntakeAgent(llm),
        CustomerAgent(),
        OrderProductAgent(),
        PaymentAgent(),
        DeliveryAgent(),
        PolicyAgent(llm),
        CriticAgent(llm),
        VerifierAgent(),
    ):
        bus.register(agent)

    tracer.emit(
        "run_start",
        model=config.MODEL_NAME,
        llm_enabled=llm.enabled,
        cases=len(cases),
        agents=bus.roster,
        policy_version=config.POLICY_VERSION,
    )

    coordinator = Coordinator(bus, tracer, run_id)
    print(f"[{run_id}] investigating {len(cases)} case(s) with {args.workers} worker(s) ...")

    if args.workers > 1:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            outcomes = list(pool.map(coordinator.run_case, cases))
    else:
        outcomes = [coordinator.run_case(case) for case in cases]

    outcomes.sort(key=lambda o: o.case_id)
    written, failed, rejected = 0, [], []
    issues: Counter[str] = Counter()
    for outcome in outcomes:
        if outcome.document is None:
            # Gated at the door: the request itself was not acceptable.
            rejected.append((outcome.case_id, outcome.errors or ["no document produced"]))
        if outcome.document is None or outcome.errors:
            failed.append((outcome.case_id, outcome.errors or ["no document produced"]))
        if outcome.document is not None:
            (config.OUTPUT_DIR / f"{outcome.case_id}.json").write_text(
                json.dumps(outcome.document, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            written += 1
        if outcome.primary_issue:
            issues[outcome.primary_issue] += 1

    elapsed = round(time.perf_counter() - started, 2)
    disagreements = [o.case_id for o in outcomes if o.adjudicator_agrees is False]
    critiqued = [o.case_id for o in outcomes if o.critic_findings]
    repaired = [o.case_id for o in outcomes if o.repair_rounds]

    tracer.emit(
        "run_end",
        cases=len(outcomes), written=written, failed=len(failed),
        elapsed_s=elapsed, llm=llm.usage, primary_issue_distribution=dict(issues),
    )

    metadata = {
        "run_id": run_id,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": {
            "name": config.MODEL_NAME,
            "provider": config.MODEL_PROVIDER,
            "parameter_size": config.MODEL_PARAM_SIZE,
            "temperature": config.TEMPERATURE,
            "seed": config.SEED,
            "max_output_tokens": config.MAX_OUTPUT_TOKENS,
            "response_format": "json_object",
        },
        "framework": {
            "orchestration": "custom A2A message bus (src/a2a) - no agent framework dependency",
            "llm_sdk": f"openai-python {_sdk_version()}",
            "language": f"python {sys.version.split()[0]}",
            "dependencies": ["openai", "python-dotenv"],
        },
        "runtime": {
            "elapsed_s": elapsed,
            "workers": args.workers,
            "cases": len(outcomes),
            "outputs_written": written,
            "hard_gate_failures": len(failed),
            "requests_rejected": len(rejected),
            "llm_enabled": llm.enabled,
        },
        "agents": bus.roster,
        "dataset": {"source": "Brazilian E-Commerce Public Dataset by Olist", **store.stats},
        "policy_version": config.POLICY_VERSION,
        "interpretation_switches": {
            "category_language": config.CATEGORY_LANGUAGE,
            "ranked_causes": config.RANKED_CAUSES,
            "evidence_sellers": config.EVIDENCE_SELLERS,
            "output_dir": config.OUTPUT_DIR.name,
        },
        "llm_usage": llm.usage,
        "results": {
            "rejected_requests": [c for c, _ in rejected],
            "primary_issue_distribution": dict(sorted(issues.items())),
            "adjudicator_disagreements": disagreements,
            "cases_with_critic_findings": critiqued,
            "cases_repaired": repaired,
            "failed_cases": [c for c, _ in failed],
        },
        "trace": {"path": "logging/trace.jsonl", "lines": tracer.lines, "events": tracer.counts},
    }
    config.METADATA_PATH.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    tracer.close()

    print(f"\n[{run_id}] done in {elapsed}s")
    print(f"  outputs written : {written}/{len(outcomes)} -> {config.OUTPUT_DIR}")
    print(f"  primary issues  : {dict(sorted(issues.items()))}")
    print(f"  llm usage       : {llm.usage}")
    print(f"  adjudicator disagreements : {len(disagreements)} {disagreements or ''}")
    print(f"  critic findings on cases  : {len(critiqued)} {critiqued or ''}")
    print(f"  repaired cases            : {len(repaired)} {repaired or ''}")
    if rejected:
        print(f"  ! REQUESTS REJECTED AT THE INPUT GATE ({len(rejected)}):")
        for case_id, errs in rejected:
            print(f"      {case_id}: {errs[:2]}")
    if failed:
        print(f"  ! SCHEMA FAILURES ({len(failed)}):")
        for case_id, errs in failed:
            print(f"      {case_id}: {errs[:3]}")
        return 2
    print("  schema: all cases clean")
    return 0


def _sdk_version() -> str:
    try:
        import openai

        return openai.__version__
    except Exception:
        return "unavailable"


if __name__ == "__main__":
    raise SystemExit(main())
