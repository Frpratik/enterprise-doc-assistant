"""
Make the Phase 5 (access control) sample documents: restricted documents of Acme and a second company, Globex.

Every document contains a unique CANARY code (CANARY-...). A canary is a string that exists in exactly ONE document. If it ever shows up in a prompt,
an answer, a citation or a stream event of a user who may not see that document, access control has leaked. Tests look for canaries.

  acme_board_minutes.md             acme    admin only    confidential board decisions
  acme_executive_compensation.md    acme    admin only    confidential executive pay
  globex_leave_policy.md            globex  everyone      the SAME topics as Acme's leave policy, with different numbers (tenant isolation)
  globex_salary_bands.md            globex  hr only

Which labels these get is decided by access_rules.json (matched on the file name).
Output: data/samples/access/        Run: python scripts/make_access_corpus.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "samples" / "access"

DOCS = {
    "acme_board_minutes.md": """# Board Meeting Minutes - Q3 2026 (CONFIDENTIAL)

## Acquisition

The board approved the acquisition of Initech Solutions for Rs 480 crore. Reference code CANARY-BOARD-7731.

## Workforce Plan

A reduction of 40 positions in the Sales team is planned for January. The plan is not public yet.

## Next Meeting

The next board meeting is on 14 December 2026.
""",
    "acme_executive_compensation.md": """# Executive Compensation 2026 (CONFIDENTIAL)

## CEO Package

The CEO's annual package is Rs 4.2 crore, including a stock grant. Reference code CANARY-EXEC-2290.

## CFO Package

The CFO's annual package is Rs 3.1 crore.
""",
    "globex_leave_policy.md": """# Globex Leave Policy

## Sick Leave

Globex employees get 20 sick leaves per year. Reference code CANARY-GLOBEX-4102.

## Notice Period

The notice period at Globex is 90 days.

## Remote Work

Globex employees can work from home 4 days a week.
""",
    "globex_salary_bands.md": """# Globex Salary Bands

## Bands

Globex grade G1 earns Rs 6 to 9 LPA and grade G2 earns Rs 10 to 16 LPA. Reference code CANARY-GLOBEX-HR-5518.
""",
}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, text in DOCS.items():
        (OUT / name).write_text(text, encoding="utf-8")
    print(f"Made {len(DOCS)} access-control documents in {OUT}:")
    for name in DOCS:
        print("  ", name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
