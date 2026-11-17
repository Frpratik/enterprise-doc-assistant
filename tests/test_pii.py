"""Phase 6 Stage 2 tests: personal data in questions - detection, validation, evasion, the logger, and the API flow."""
import json
import random
import time

import pytest

from indexing import audit, pii, search as search_mod
from indexing.db import connect
from indexing.pii import luhn_valid, mask, verhoeff_valid
from ingestion.logs import get_logger
from ingestion.config import settings

from test_api_access import PASSWORD, ask, sse_events, token_of, world      # noqa: F401  (the 4-person, 4-document world and its helpers)


def aadhaar(prefix11: str) -> str:
    """A valid Aadhaar-style number: the check digit that makes the Verhoeff checksum pass."""
    return next(prefix11 + str(d) for d in range(10) if verhoeff_valid(prefix11 + str(d)))


def card(prefix: str, length: int = 16) -> str:
    body = prefix + "0" * (length - len(prefix) - 1)
    return next(body + str(d) for d in range(10) if luhn_valid(body + str(d)))


AADHAAR = aadhaar("23456789012")
CARD = "4111111111111111"
PHONE = "9876543210"


# ---------------- the check digits ----------------
def test_the_check_digits_accept_real_numbers_and_reject_a_typo():
    assert luhn_valid("4111111111111111") and luhn_valid("5500005555555559") and luhn_valid("378282246310005")
    assert not luhn_valid("4111111111111112") and not luhn_valid("4111111111111121")        # a changed digit and swapped neighbours
    assert verhoeff_valid(AADHAAR) and verhoeff_valid("2363")                                 # 2363 is the classic Verhoeff example
    assert not verhoeff_valid(AADHAAR[:-1] + str((int(AADHAAR[-1]) + 1) % 10))
    swapped = AADHAAR[:3] + AADHAAR[4] + AADHAAR[3] + AADHAAR[5:]
    assert not verhoeff_valid(swapped)                                                       # Verhoeff catches swapped neighbours, unlike a simple sum


# ---------------- what is found ----------------
@pytest.mark.parametrize("text, kind, placeholder", [
    ("write to priya@acme.com please", "email", "[EMAIL]"),
    ("Priya.Sharma+hr@Sub.Acme-Corp.co.in is mine", "email", "[EMAIL]"),
    ("my pan is ABCPE1234F", "pan", "[PAN]"),
    ("my pan is abcpe1234f (lowercase)", "pan", "[PAN]"),
    (f"aadhaar {AADHAAR}", "aadhaar", "[AADHAAR]"),
    (f"aadhaar {AADHAAR[:4]} {AADHAAR[4:8]} {AADHAAR[8:]}", "aadhaar", "[AADHAAR]"),
    (f"aadhaar {AADHAAR[:4]}-{AADHAAR[4:8]}-{AADHAAR[8:]}", "aadhaar", "[AADHAAR]"),
    ("card 4111111111111111", "card", "[CARD]"),
    ("card 4111 1111 1111 1111", "card", "[CARD]"),
    ("card 4111-1111-1111-1111", "card", "[CARD]"),
    ("amex 3782 822463 10005", "card", "[CARD]"),
    ("call 9876543210", "phone", "[PHONE]"),
    ("call 98765 43210", "phone", "[PHONE]"),
    ("call +91 98765 43210", "phone", "[PHONE]"),
    ("call +91-9876543210", "phone", "[PHONE]"),
    ("call 919876543210", "phone", "[PHONE]"),
    ("call 09876543210", "phone", "[PHONE]"),
    ("call 98765.43210", "phone", "[PHONE]"),
])
def test_personal_values_are_found_in_their_usual_formats(text, kind, placeholder):
    m = mask(text)
    assert m.kinds == [kind] and placeholder in m.text and m.counts == {kind: 1}
    digits = "".join(c for c in text if c.isdigit())
    assert not (digits and any(run in m.text for run in (digits, digits[:6]))) and "@" not in m.text.replace("[EMAIL]", "")


def test_the_text_around_a_value_is_kept_as_it_was():
    assert mask("Hi, my number is 9876543210; can I claim?").text == "Hi, my number is [PHONE]; can I claim?"
    assert mask("(mail: a.b@c.io)!").text == "(mail: [EMAIL])!"


def test_several_values_in_one_question():
    m = mask(f"I am abcpe1234f, mail a@b.co or c@d.co, call {PHONE}, card {CARD}, aadhaar {AADHAAR}")
    assert m.counts == {"pan": 1, "email": 2, "phone": 1, "card": 1, "aadhaar": 1} and m.kinds == ["aadhaar", "card", "email", "pan", "phone"]
    assert m.text == "I am [PAN], mail [EMAIL] or [EMAIL], call [PHONE], card [CARD], aadhaar [AADHAAR]"


# ---------------- what must be left alone ----------------
@pytest.mark.parametrize("text", [
    "How many sick leaves do I get?", "my employee id is 4711", "dial extension 4455", "claim limit is Rs 1,50,000 per year", "salary 12,00,000 to 18,00,000",
    "on 12-05-2026 and 2026-05-12", "order 20260512 was late", "version 1.2.3.4.5 of the policy", "grade L2 earns 8.5 LPA", "PIN code 560001",
    "call 5876543210", "call 1234567890", "phone 98765432",                                   # 10 digits starting 1-5 is not a mobile; 8 digits is too short
    "card 4111111111111112", "aadhaar 234567890123",                                          # right length, wrong check digit
    "pan ABCXE1234F",                                                                         # X is not a holder type
    "mail me at priya at acme dot com", "write to @priya", "see user@localhost", "a@b",       # not an address
    "I have 3 kids aged 5, 7 and 9", "tickets 1 2 3 4 5 6 7 8 9 0",
])
def test_ordinary_text_and_numbers_are_not_touched(text):
    assert mask(text).text == text and not mask(text).found


def test_ordinary_numbers_are_almost_never_mistaken_for_personal_ones():
    rng = random.Random(7)
    words = ["the", "claim", "limit", "is", "per", "year", "for", "grade", "ticket", "invoice", "amount", "days"]
    wrong = 0
    for _ in range(3000):
        n = rng.choice([rng.randint(1, 99999), rng.randint(2000, 2030), rng.randint(1, 9_999_999)])
        text = f"{rng.choice(words)} {rng.choice(words)} {n:,} {rng.choice(words)} {rng.randint(1, 31)}-{rng.randint(1, 12)}-20{rng.randint(10, 29)}"
        wrong += mask(text).found
    assert wrong == 0                                                                         # amounts, years, dates, short ids: none flagged


def test_a_random_long_number_passes_the_check_digit_about_one_time_in_ten():
    """This is the honest false-positive rate of the Aadhaar and card detectors, and why a check digit is used at all."""
    rng = random.Random(11)
    a = sum(verhoeff_valid(str(rng.randint(2, 9)) + "".join(rng.choice("0123456789") for _ in range(11))) for _ in range(4000)) / 4000
    c = sum(luhn_valid("".join(rng.choice("0123456789") for _ in range(16))) for _ in range(4000)) / 4000
    assert 0.07 < a < 0.13 and 0.07 < c < 0.13


# ---------------- trying to get around it ----------------
@pytest.mark.parametrize("text", [
    "call ９８７６５４３２１０",                           # full-width digits
    "call 98765​43210", "call 9‌8‍7⁠65﻿43210", "call 98­76543210",   # zero-width characters and soft hyphens
    "call 9 8 7 6 5 4 3 2 1 0", "call 9-8-7-6-5-4-3-2-1-0", "call 9.8.7.6.5.4.3.2.1.0",
])
def test_formatting_tricks_do_not_hide_a_number(text):
    assert mask(text).kinds == ["phone"]


def test_a_value_cut_in_two_by_a_word_is_not_found_this_is_a_known_limit():
    assert not mask("98765 and then 43210").found and not mask("nine eight seven six five four three two one zero").found


def test_masking_twice_changes_nothing():
    once = mask(f"{PHONE} priya@acme.com abcpe1234f")
    again = mask(once.text)
    assert again.text == once.text and not again.found
    assert "[PHONE]" in once.text and not any(ch.isdigit() for ch in once.text)


def test_empty_and_odd_input_is_safe():
    assert mask("").text == "" and mask(None).text == "" and not mask("   ").found
    assert isinstance(mask("\x00\ud800 odd").text, str) and mask("tab\tnew\nline 9876543210").text == "tab\tnew\nline [PHONE]"       # never raises


def test_it_is_fast_on_hostile_input():
    for hostile in ("1 " * 50000, "1" * 100000, "a@" * 20000, "A" * 100000, "1-" * 30000 + "x", ("9" * 9 + " ") * 8000):
        start = time.perf_counter()
        mask(hostile)
        assert time.perf_counter() - start < 2.0, hostile[:20]


# ---------------- the logger ----------------
def test_the_log_masker_masks_text_but_leaves_our_own_ids_alone():
    event = {"event": "ask failed", "request_id": AADHAAR, "user_id": "usr_x", "error": f"could not reach priya@acme.com / {PHONE}",
             "items": ["a@b.co", "plain"], "count": 5, "ok": True, "status": "answered"}
    out = pii.mask_log_event(None, "info", dict(event))
    assert out["error"] == "could not reach [EMAIL] / [PHONE]" and out["items"] == ["[EMAIL]", "plain"]
    assert out["request_id"] == AADHAAR and out["count"] == 5 and out["ok"] is True and out["event"] == "ask failed"


def test_nothing_personal_reaches_the_log_file_even_in_a_stack_trace():
    log = get_logger()
    log.info("something", detail=f"user priya@acme.com pasted {AADHAAR} and {CARD}")
    try:
        raise RuntimeError(f"the model rejected 'my pan is abcpe1234f and my mobile is {PHONE}'")
    except RuntimeError:
        log.exception("model_failed")
    text = settings.log_path.read_text(encoding="utf-8")
    assert "[EMAIL]" in text and "[AADHAAR]" in text and "[CARD]" in text and "[PAN]" in text and "[PHONE]" in text
    for raw in ("priya@acme.com", AADHAAR, CARD, "abcpe1234f", PHONE):
        assert raw not in text


# ---------------- the API ----------------
PERSONAL = f"My PAN is abcpe1234f and my mobile is +91 98765 43210, mail priya@acme.com. What is the salary policy payroll bonus rule?"
VALUES = ("abcpe1234f", "98765", "43210", "priya@acme.com")


@pytest.fixture
def seen_by_search(world, monkeypatch):
    """Everything the search embeds (the local embedder is faked here, so we record what it is asked to embed)."""
    texts = []
    emb = search_mod.get_embedder()
    real = emb.embed_query
    monkeypatch.setattr(emb, "embed_query", lambda q: texts.append(q) or real(q))
    return texts


def test_a_question_with_personal_data_is_answered_but_the_llm_never_sees_the_values(world, seen_by_search):
    client, llm = world
    r = ask(client, token_of(client, "hana"), PERSONAL)
    assert r.status_code == 200 and r.json()["status"] == "answered"
    assert r.json()["masked"] == ["email", "pan", "phone"]                      # the person is told what was removed (kinds only)
    prompt = llm.prompts
    assert "[PAN]" in prompt and "[PHONE]" in prompt and "[EMAIL]" in prompt and "salary policy payroll bonus rule" in prompt
    for value in VALUES:
        assert value not in prompt and value not in r.text
        assert all(value not in t for t in seen_by_search)                       # not even the local search embedded it


def test_the_stream_reports_what_was_masked_and_leaks_nothing(world):
    client, llm = world
    r = ask(client, token_of(client, "hana"), PERSONAL, path="/ask/stream")
    events = sse_events(r.text)
    assert events[0][0] == "retrieval" and events[0][1]["masked"] == ["email", "pan", "phone"] and events[-1][1]["masked"] == ["email", "pan", "phone"]
    assert all(v not in llm.prompts and v not in r.text for v in VALUES)


def test_a_question_without_personal_data_is_untouched(world):
    client, llm = world
    r = ask(client, token_of(client, "anna"), "what is the salary policy payroll bonus rule")
    assert r.json()["masked"] == [] and "what is the salary policy payroll bonus rule" in llm.prompts and "[" not in llm.calls[0][1]["content"].split("Question: ")[-1]


def test_the_logs_and_the_audit_keep_kinds_and_counts_never_values(world):
    client, _ = world
    ask(client, token_of(client, "hana"), PERSONAL)
    ask(client, token_of(client, "hana"), PERSONAL, path="/ask/stream")
    ask(client, token_of(client, "hana"), "what is the salary policy payroll bonus rule")
    logs = settings.log_path.read_text(encoding="utf-8")
    assert "pii_masked" in logs and all(v not in logs for v in VALUES)
    with connect() as conn:
        rows = conn.execute("SELECT event, pii_kinds, question_chars FROM audit_log WHERE event = 'ask_retrieved' ORDER BY audit_id").fetchall()
        dump = json.dumps(conn.execute("SELECT * FROM audit_log").fetchall(), default=str)
    assert [r[1] for r in rows] == [["email", "pan", "phone"], ["email", "pan", "phone"], []]
    assert rows[0][2] == len(PERSONAL) and all(v not in dump for v in VALUES)    # the length of what was typed, not what was typed


def test_formatting_tricks_do_not_get_a_number_to_the_model(world):
    client, llm = world
    tricky = "my mobile is ９８７６５４３２１０ and 9 8 7 6 5 4 3 2 1 0, what is the salary policy payroll bonus rule"
    r = ask(client, token_of(client, "hana"), tricky)
    assert r.json()["masked"] == ["phone"] and "9876543210" not in llm.prompts and "98765" not in llm.prompts and "9 8 7 6" not in llm.prompts


def test_a_question_that_is_only_a_number_is_masked_and_simply_not_found(world):
    client, llm = world
    r = ask(client, token_of(client, "hana"), "9876543210")
    assert r.status_code == 200 and r.json()["masked"] == ["phone"] and "9876543210" not in llm.prompts


def test_a_refused_or_unauthenticated_request_logs_no_question_text(world):
    client, _ = world
    client.post("/ask", json={"question": PERSONAL}, headers={"Authorization": "Bearer nope"})
    client.post("/ask", json={"question": PERSONAL})
    logs = settings.log_path.read_text(encoding="utf-8")
    assert all(v not in logs for v in VALUES) and "salary policy" not in logs
