"""
Ask a question from the terminal and get an answer with sources.

  python -m indexing.ask "how many sick leaves do I get"
  python -m indexing.ask "what is the notice period" --show-chunks

Needs: the database (docker compose up -d), documents indexed, and GROQ_API_KEY in .env.
"""
import argparse
import sys

from indexing.answer import answer
from indexing.llm import LLMAuthError


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="python -m indexing.ask")
    ap.add_argument("question")
    ap.add_argument("--show-chunks", action="store_true", help="also show the chunks the LLM was given")
    args = ap.parse_args(argv)

    from indexing.db import can_connect
    if not can_connect():
        print("Database not reachable: start it with  docker compose up -d")
        return 1
    try:
        a = answer(args.question)
    except LLMAuthError as e:
        print(f"LLM key problem: {e}")
        return 1

    print(f'\nQ: "{args.question}"\n')
    print(a.text)
    if a.citations:
        print("\nSources:")
        for c in a.citations:
            print(f"  [{c.n}] {c.file_name} > {c.heading_path.split(' > ', 1)[-1]}" + (f"  ({c.where})" if c.where else ""))
    note = {"refused_gate": "stopped by the score gate (no LLM call)", "refused_llm": "the LLM found no answer in the excerpts",
            "uncited": "the LLM answered without a valid citation, so it was not shown", "llm_unavailable": "every model failed"}.get(a.status)
    print(f"\n[{a.status}{': ' + note if note else ''}"
          f"{' | model ' + a.model if a.model else ''}{' (fallback used)' if a.fell_back else ''}"
          f"{' | ' + str(a.latency_ms) + ' ms' if a.latency_ms else ''}]")
    if args.show_chunks:
        for i, h in enumerate(a.hits, 1):
            print(f"\n--- [{i}] {h.file_name} > {h.heading_path}\n{h.content}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
