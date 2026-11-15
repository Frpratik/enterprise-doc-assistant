"""
Build evals/judge_calibration.json: the data that tells us how far the LLM judge can be trusted (Phase 4 Stage 3).

  30 REAL answers (from the saved qwen baseline) with labels written BY HAND: the question, the reference, the answer and the full excerpts
  were read, and each answer was labelled BEFORE the judge ever saw it.
       faithful   every claim in the answer is supported by the excerpts the assistant was given      (True / False)
       correct    matches the reference for everything the question asks                            (correct / partial / incorrect)
       relevant   addresses the question                                                               (direct / partial / off_topic)
  32 NEGATIVE CONTROLS: real, correct answers deliberately broken in a known way (wrong number, invented sentence, answer to another question,
  a 'not found' reply). Their labels are true by construction. Real answers are almost all correct, so without controls we could never
  find out whether the judge CATCHES a bad answer.

HONEST NOTE ON THE LABELS: they were written by the AI assistant that also helped build this project, not by an independent human expert.
They are careful, but they share any blind spot of the same kind of reader. A human spot-check of the 'note' items is the best next step.

  python scripts/build_judge_calibration.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from indexing.answer_golden import load_golden  # noqa: E402
from indexing.answer_judge import CALIBRATION, build_controls  # noqa: E402

BASELINE = ROOT / "evals" / "answers_v2_baseline.json"

OK = (True, "correct", "direct")
# id -> (faithful, correct, relevant, note)
LABELS = {
    "A011": (*OK, "matches the reference; the QUESTION is ambiguous (highest attrition RATE is Sales, the most people lost is Engineering)"),
    "A014": (*OK, ""), "A016": (*OK, "adds the spouse / children detail that is in the excerpt"), "A020": (*OK, ""), "A024": (*OK, ""),
    "A034": (*OK, ""), "A036": (*OK, ""), "A039": (*OK, ""), "A040": (*OK, ""), "A041": (*OK, ""), "A042": (*OK, "follows from the table range"),
    "A048": (*OK, ""), "A050": (*OK, ""), "A051": (*OK, ""),
    "A056": (*OK, "the reference also states 1 July, which the question did not ask"),
    "A058": (*OK, ""), "A060": (*OK, ""), "A062": (*OK, ""), "A064": (*OK, ""), "A072": (*OK, "leaves out the GST / PAN detail, all six steps are there"),
    "A077": (*OK, "AMBIGUOUS: the reference adds the 6-month condition, the question asks only how much"),
    "A083": (*OK, ""), "A084": (*OK, ""),
    "A085": (True, "partial", "direct", "FLAWED: misses the 4-hour security-incident report to security@acme-example.com"),
    "A086": (*OK, ""), "A087": (*OK, "arithmetic: 12,000 + 25,000 = 37,000 follows directly from the two excerpts"),
    "A088": (*OK, ""), "A089": (*OK, ""), "A090": (*OK, ""), "A093": (*OK, ""),
}


def main() -> int:
    golden = load_golden()
    q_by_id = {q.id: q for q in golden.questions}
    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    records = {r["id"]: r for r in base["records"]}
    items = []
    for qid, (faithful, correct, relevant, note) in LABELS.items():
        r, q = records[qid], q_by_id[qid]
        assert r["status"] == "answered", qid
        items.append({"id": f"real:{qid}", "source": "real", "question": q.q, "reference": q.reference, "answer": r["text"], "context": r["context"],
                      "labels": {"faithful": faithful, "correct": correct, "relevant": relevant}, **({"note": note} if note else {})})
    controls = build_controls(list(records.values()), golden)
    items += controls
    kinds = {}
    for c in controls:
        kinds[c["source"]] = kinds.get(c["source"], 0) + 1
    changelog = [
        "v2: the non_answer controls were first labelled faithful=True (an answer with no claims). The judge disagreed on 6 of 8, and on reflection it was right: "
        "each was built from a question whose excerpts DO contain the answer, so 'there is no information about this' is a false claim about the excerpts. "
        "Relabelled faithful=False. Numbers with the first labels (agreement 90%) are kept in docs/BUILD_LOG.md."]
    doc = {"about": ("Calibration data for the LLM judge. 'real' items: qwen answers labelled by hand before the judge saw them. Other items: negative controls, "
                     "real answers broken in a known way; labels true by construction. Labels were written by the AI assistant, not an independent human."),
           "version": 2, "changelog": changelog, "answers_from": f"{base['meta']['model']} (evals/answers_v2_baseline.json)", "items": items}
    CALIBRATION.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    flawed = [i for i in items if i["source"] == "real" and (not i["labels"]["faithful"] or i["labels"]["correct"] != "correct")]
    print(f"wrote {CALIBRATION.name}: {len(items)} items = {len(LABELS)} real ({len(flawed)} flawed) + {len(controls)} negative controls {kinds}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
