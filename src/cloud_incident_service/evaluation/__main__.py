"""Run the evaluation scenarios: `python -m cloud_incident_service.evaluation [--json]`."""

from __future__ import annotations

import argparse
import asyncio
import sys

from cloud_incident_service.evaluation.models import EvaluationReport
from cloud_incident_service.evaluation.runner import run_evaluation


def format_table(report: EvaluationReport) -> str:
    lines = [f"Orchestrator: {report.orchestrator} ({report.model})", ""]
    width = max(len(result.scenario_id) for result in report.results)
    for result in report.results:
        verdict = "PASS" if result.passed else "FAIL"
        lines.append(f"{verdict}  {result.scenario_id:<{width}}  status={result.report_status}")
        for name, check in result.checks.items():
            if not check.passed:
                lines.append(f"      {name}: {check.detail}")
    lines += ["", f"Passed {report.passed}/{len(report.results)} ({report.pass_rate:.0%})"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the incident evaluation scenarios.")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    parser.add_argument(
        "--min-pass-rate",
        type=float,
        default=1.0,
        help="exit with status 1 when the pass rate is below this value (default 1.0)",
    )
    args = parser.parse_args(argv)
    if not 0.0 <= args.min_pass_rate <= 1.0:
        parser.error("--min-pass-rate must be between 0 and 1")

    report = asyncio.run(run_evaluation())
    print(report.model_dump_json(indent=2) if args.json else format_table(report))
    return 0 if report.pass_rate >= args.min_pass_rate else 1


if __name__ == "__main__":
    sys.exit(main())
