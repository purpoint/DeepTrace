"""Command-line entry point.

Its job at this stage is diagnostic: prove the package imports, configuration
resolves, and logging works, without needing a database, a queue, or an API key.
That makes "the application starts" a claim anyone can verify on a fresh clone
rather than something the README asserts.

Research commands are added by the milestones that implement them.
"""

from __future__ import annotations

import argparse
import sys

from core.config import DEPTH_BUDGETS, Settings, get_settings
from core.logging import configure_logging, get_logger

__version__ = "0.1.0"


def _mask(value: str | None) -> str:
    """Report whether a credential is configured without revealing it."""
    return "configured" if value else "not set"


def _print_status(settings: Settings) -> None:
    """Print resolved configuration, including which credentials are present.

    Values are never printed -- only presence. This is the same discipline the
    log redaction enforces, applied to terminal output.
    """
    print(f"DeepTrace {__version__}")
    print()
    print("Application")
    print(f"  environment            {settings.app_env.value}")
    print(f"  log level              {settings.log_level}")
    print(f"  json logs              {settings.json_logs}")
    print()
    print("Model routing")
    print(f"  provider               {settings.llm_provider}")
    print(f"  cheap tier             {settings.llm_model_cheap}")
    print(f"  strong tier            {settings.llm_model_strong}")
    print(f"  embeddings             {settings.llm_model_embed}")
    print()
    print("Credentials")
    print(f"  openai                 {_mask(settings.openai_api_key)}")
    print(f"  anthropic              {_mask(settings.anthropic_api_key)}")
    print(f"  google                 {_mask(settings.google_api_key)}")
    print(f"  tavily                 {_mask(settings.tavily_api_key)}")
    print()
    print("Research limits")
    print(f"  default depth          {settings.default_depth.value}")
    print(f"  max concurrent tasks   {settings.max_concurrent_tasks}")
    print(f"  max graph iterations   {settings.max_graph_iterations}")
    print()
    print("Depth budgets")
    header = f"  {'depth':<10} {'tasks':>6} {'sources':>8} {'loops':>6} {'tokens':>9}"
    print(header)
    for depth, budget in DEPTH_BUDGETS.items():
        print(
            f"  {depth.value:<10} {budget.max_tasks:>6} {budget.max_sources:>8} "
            f"{budget.max_verification_loops:>6} {budget.max_tokens:>9,}"
        )


def _run_check(settings: Settings) -> int:
    """Verify the foundation is wired correctly. Returns a process exit code."""
    log = get_logger("deeptrace.cli")

    log.info("startup.check", environment=settings.app_env.value, version=__version__)

    problems: list[str] = []
    if not any((settings.openai_api_key, settings.anthropic_api_key, settings.google_api_key)):
        problems.append("No LLM provider key is set. Add one to .env before running research.")
    if not settings.tavily_api_key:
        problems.append("No search provider key is set. Add TAVILY_API_KEY to .env.")

    print(f"configuration            loaded ({settings.app_env.value})")
    print(f"logging                  configured (level {settings.log_level})")
    print(f"depth budgets            {len(DEPTH_BUDGETS)} defined")

    if problems:
        print()
        for problem in problems:
            print(f"pending: {problem}")
        print()
        print("The foundation is healthy. Research requires the keys listed above.")
    else:
        print()
        print("All foundation checks passed.")

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deeptrace",
        description="Autonomous AI research and evidence synthesis platform.",
    )
    parser.add_argument("--version", action="version", version=f"deeptrace {__version__}")

    subcommands = parser.add_subparsers(dest="command")
    subcommands.add_parser("status", help="Show resolved configuration and depth budgets")
    subcommands.add_parser("check", help="Verify the foundation is correctly wired")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point registered as the ``deeptrace`` console script."""
    parser = build_parser()
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings)

    if args.command == "status":
        _print_status(settings)
        return 0
    if args.command == "check":
        return _run_check(settings)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
