#!/usr/bin/env python3
"""Regression test for the entity gate, using the real cases that exposed it.

    python tests/test_entity_gate.py            # deterministic filters only
    python tests/test_entity_gate.py --with-llm # also exercise the model path

The deterministic run needs no API key and is the one to keep in CI: the
three non-targets in the corpus are all caught by STRONG patterns, and the
control case must survive. The --with-llm run additionally proves the model
path agrees and that its evidence quote is really on the page.

Exits non-zero on any failure.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config
from gather import entity
from model import CompanyResult

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "adversarial_leads.json"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Entity-gate regression test.")
    ap.add_argument("--with-llm", action="store_true",
                    help="also run the model path (needs ANTHROPIC_API_KEY)")
    args = ap.parse_args(argv)

    config.load_dotenv()
    engine = None
    if args.with_llm:
        from llm import AnthropicEngine
        engine = AnthropicEngine()

    cases = json.loads(FIXTURES.read_text(encoding="utf-8"))["cases"]
    failures = 0
    print(f"entity gate: {len(cases)} adversarial case(s), "
          f"{'with' if engine else 'without'} model\n")

    for case in cases:
        result = CompanyResult(company=case["company"], domain=case["domain"],
                               industry="insurance")
        result.coverage["site"] = "ok"
        entity.check(result, case["homepage_text"], engine)

        ok = result.is_target == case["expect_is_target"]
        # Type only has to match when we expected a disqualification; a target
        # may legitimately come back "target" or "unclear".
        if not case["expect_is_target"]:
            ok = ok and result.entity_type == case["expect_entity_type"]
            ok = ok and bool(result.entity_evidence.strip())
            if result.entity_evidence:
                norm = lambda s: " ".join(s.split()).lower()  # noqa: E731
                if norm(result.entity_evidence) not in norm(case["homepage_text"]):
                    ok = False
                    print(f"    evidence not found verbatim on the page")

        status = "PASS" if ok else "FAIL"
        if not ok:
            failures += 1
        print(f"[{status}] {case['company']}")
        print(f"        expected is_target={case['expect_is_target']} "
              f"type={case['expect_entity_type']}")
        print(f"        got      is_target={result.is_target} "
              f"type={result.entity_type}")
        if result.entity_evidence:
            print(f'        evidence "{result.entity_evidence[:110]}"')
        if case.get("was_ranked"):
            print(f"        before the gate existed: {case['was_ranked']}")
        print()

    print(f"result: {'ALL CASES PASSED' if failures == 0 else f'{failures} CASE(S) FAILED'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
