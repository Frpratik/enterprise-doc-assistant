"""
Make the HARDER evaluation documents (Phase 4): 10 synthetic company documents for "Acme Example Pvt Ltd".

They are designed with TRAPS on purpose, because the first 12 sample documents were too easy:
  * look-alike facts in different documents:   travel claims 15 days  vs  general expense claims 30 days
                                              device loss 2 hours    vs  security incident 4 hours
                                              gift limit Rs 2,000    vs  client meal limit Rs 2,000
                                              learning budget Rs 25,000  vs  referral bonus Rs 25,000
  * the same topic in several places:         harassment (old code of conduct + new one), appraisals (handbook + policy)
  * answers that sit in ONE CELL of a table:  hotel limits (tier x grade), salary increments (rating x grade)
  * long PDFs with repeated headers / footers, a numbered procedure, a Markdown table
  * topics next to what employees will ask but are NOT answered (so "I don't know" can be tested)

Output: data/samples/eval2/   (separate from data/samples/good, so the Phase 1-3 tests and evals are untouched)
Run:    python scripts/make_eval_corpus.py
"""
import sys
from pathlib import Path

import pandas as pd
from docx import Document
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "samples" / "eval2"
styles = getSampleStyleSheet()
COMPANY_HEADER = "ACME EXAMPLE PVT LTD - INTERNAL USE ONLY"


# ---------------- helpers ----------------
def pdf_table(rows):
    t = Table(rows)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black), ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey)]))
    return t


def make_pdf(name, title, sections, break_every=0):
    """sections = [(heading, [paragraph text | table rows, ...]), ...]. Every page gets the same header and a page number."""
    def decorate(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.drawString(50, A4[1] - 30, COMPANY_HEADER)
        canvas.drawRightString(A4[0] - 50, 25, f"Page {doc.page}")
        canvas.restoreState()

    flow = [Paragraph(title, styles["Title"]), Spacer(1, 10)]
    for i, (heading, items) in enumerate(sections):
        if break_every and i and i % break_every == 0:
            flow.append(PageBreak())
        flow.append(Paragraph(heading, styles["Heading2"]))
        for item in items:
            flow.append(pdf_table(item) if isinstance(item, list) else Paragraph(item, styles["BodyText"]))
            flow.append(Spacer(1, 8))
    SimpleDocTemplate(str(OUT / name), pagesize=A4, title=title, author="Acme Example").build(
        flow, onFirstPage=decorate, onLaterPages=decorate)


def make_docx(name, title, blocks):
    """blocks = [("h", text) | ("p", text) | ("table", rows) | ("num", text)]"""
    doc = Document()
    doc.add_heading(title, level=1)
    for kind, value in blocks:
        if kind == "h":
            doc.add_heading(value, level=2)
        elif kind == "p":
            doc.add_paragraph(value)
        elif kind == "num":
            doc.add_paragraph(value, style="List Number")
        elif kind == "table":
            t = doc.add_table(rows=len(value), cols=len(value[0]))
            t.style = "Table Grid"
            for r, row in enumerate(value):
                for c, cell in enumerate(row):
                    t.cell(r, c).text = str(cell)
    doc.save(str(OUT / name))


# ---------------- the documents ----------------
def travel_policy():
    make_docx("travel_policy.docx", "Travel Policy 2026", [
        ("h", "Purpose"),
        ("p", "This policy explains how employees of Acme Example Pvt Ltd book business travel, what the company pays for, "
              "and how to claim travel costs."),
        ("h", "Eligibility"),
        ("p", "Employees of grade L3 and above may travel business class on flights longer than 6 hours. Employees below "
              "grade L3 travel economy class on all flights. Train travel is by AC 2-tier for all grades."),
        ("h", "Booking"),
        ("p", "International travel must be booked through the travel desk at least 21 days before departure and needs the "
              "approval of the department head. Tickets booked outside the travel desk are not reimbursed. Domestic booking "
              "rules are given in the Employee Handbook."),
        ("h", "Hotel Limits"),
        ("p", "The maximum hotel cost per night depends on the city tier and the grade of the employee. Amounts are in rupees "
              "and include taxes."),
        ("table", [["City tier", "Grades L1-L2", "Grades L3-L4", "Grades L5 and above"],
                   ["Tier 1 (Mumbai, Delhi, Bengaluru)", "4,500", "6,500", "9,000"],
                   ["Tier 2 (Pune, Hyderabad, Chennai)", "3,500", "5,000", "7,500"],
                   ["Tier 3 (all other cities)", "2,500", "3,500", "5,000"]]),
        ("h", "Daily Allowance"),
        ("p", "Employees on domestic trips receive a daily allowance of Rs 1,200 for meals and local transport. For "
              "international trips the daily allowance is USD 60."),
        ("h", "Cancellations"),
        ("p", "Trips must be cancelled at least 24 hours before departure to avoid a cancellation fee. Fees caused by personal "
              "changes are paid by the employee."),
        ("h", "Claims"),
        ("p", "Travel claims must be submitted within 15 days of returning from the trip, with tickets and hotel invoices attached."),
    ])


def expense_policy():
    make_pdf("expense_reimbursement_policy.pdf", "Expense Reimbursement Policy", [
        ("1. Eligibility", ["All permanent employees, including those on probation, can claim reasonable business expenses."]),
        ("2. Claim Deadline", ["General expense claims must be submitted within 30 days of the date of the expense. "
                               "Travel expenses follow the separate Travel Policy."]),
        ("3. Receipts", ["A receipt or invoice is mandatory for every single expense above Rs 500. Expenses of Rs 500 or less "
                         "can be claimed with a short note."]),
        ("4. Approval Limits", [[["Claim amount", "Approver"],
                                 ["Up to Rs 5,000", "Reporting manager"],
                                 ["Rs 5,001 to Rs 25,000", "Department head"],
                                 ["Above Rs 25,000", "Finance head"]]]),
        ("5. Client Entertainment", ["Client meals are reimbursed up to Rs 2,000 per person per meal. The names of the guests "
                                     "must be written on the claim."]),
        ("6. Payment", ["Approved claims are paid within 10 working days."]),
        ("7. Not Reimbursable", ["Fines, penalties, alcohol and personal entertainment are not reimbursable."]),
    ])


def it_security_policy():
    make_pdf("it_security_policy.pdf", "IT Security Policy", [
        ("1. Purpose and Scope", ["This policy protects company information and systems. It applies to every employee, "
                                  "contractor and intern who uses company devices or accounts."]),
        ("2. Passwords", ["Passwords must be at least 12 characters long and must be changed every 90 days. The last 5 passwords "
                          "cannot be reused. Passwords must never be shared, even with colleagues or the IT helpdesk."]),
        ("3. Multi-Factor Authentication", ["Multi-factor authentication is mandatory for company email, the VPN and the HR portal. "
                                            "Employees must register a second factor within 3 days of joining."]),
        ("4. Laptops and Devices", ["All company laptops must use full-disk encryption. A lost or stolen device must be reported "
                                    "to the IT helpdesk within 2 hours of the loss being noticed."]),
        ("5. Removable Media", ["USB storage devices are blocked by default on company laptops. Exceptions need written approval "
                                "from the IT head and are valid for 30 days."]),
        ("6. Security Incidents", ["Any suspected security incident, such as a phishing email that was clicked or unusual account "
                                   "activity, must be reported to security@acme-example.com within 4 hours. Do not delete the "
                                   "suspicious message."]),
        ("7. Software Installation", ["Only software from the approved software catalogue may be installed. Requests for new "
                                      "software are raised through the IT helpdesk."]),
        ("8. Remote Access", ["The VPN is mandatory whenever an employee works from outside the office network, including from home."]),
        ("9. Email and Internet Use", ["Company email must not be used for personal business. Large attachments above 25 MB "
                                       "should be shared through the approved file-sharing tool."]),
        ("10. Compliance", ["Violations of this policy may lead to disciplinary action, which can include termination of employment."]),
    ], break_every=2)


def performance_policy():
    make_docx("performance_review_policy.docx", "Performance Review Policy", [
        ("h", "Annual Review Cycle"),
        ("p", "Annual appraisals are held in April for the previous financial year. A mid-year check-in conversation takes place "
              "in October. Managers must complete all appraisals within 3 weeks of the cycle opening."),
        ("h", "Rating Scale"),
        ("p", "Ratings use a 5-point scale."),
        ("table", [["Rating", "Meaning"], ["1", "Needs Improvement"], ["2", "Partially Meets"], ["3", "Meets"],
                   ["4", "Exceeds"], ["5", "Outstanding"]]),
        ("h", "Probation Reviews"),
        ("p", "New employees on probation are reviewed at the end of month 5, and a confirmation decision is made by the end of month 6."),
        ("h", "Performance Improvement Plan"),
        ("p", "An employee who receives a rating of 1 may be placed on a Performance Improvement Plan (PIP) lasting 60 days. "
              "Progress is reviewed every 2 weeks."),
        ("h", "Promotions"),
        ("p", "Promotions are considered once a year and take effect on 1 July. Only employees rated 4 or 5 in each of the last "
              "two cycles are eligible."),
        ("h", "Salary Increments"),
        ("p", "Increments depend on the grade and the rating. See the Salary Revision Matrix."),
    ])


def salary_matrix():
    matrix = pd.DataFrame({"Rating": [5, 4, 3, 2, 1], "L1": [14, 10, 7, 3, 0], "L2": [12, 9, 6, 2, 0],
                           "L3": [10, 8, 5, 2, 0], "L4": [8, 6, 4, 1, 0]})
    notes = pd.DataFrame({"Item": ["Effective date", "Rounding"],
                          "Detail": ["Salary increments take effect from 1 July.", "Increment amounts are rounded to the nearest Rs 100."]})
    with pd.ExcelWriter(OUT / "salary_revision_matrix.xlsx") as xl:
        matrix.to_excel(xl, sheet_name="Increment Percent", index=False)
        notes.to_excel(xl, sheet_name="Notes", index=False)


def code_of_conduct():
    make_pdf("code_of_conduct_2026.pdf", "Code of Conduct 2026", [
        ("1. Our Values", ["We act with honesty, respect and accountability in everything we do for Acme Example Pvt Ltd, "
                           "our clients and each other."]),
        ("2. Gifts and Hospitality", ["Employees must not accept gifts worth more than Rs 2,000 from vendors or clients. Any gift "
                                      "above this limit must be declared to the compliance officer within 7 days."]),
        ("3. Conflict of Interest", ["Employees must disclose any outside business interest to HR before accepting it. Working "
                                     "for a competitor is not allowed."]),
        ("4. Harassment and Complaints", ["Complaints about harassment can be made to the Internal Complaints Committee at "
                                          "icc@acme-example.com. The committee acknowledges every complaint within 3 working days "
                                          "and completes its inquiry within 90 days. Retaliation against a person who complains "
                                          "is strictly prohibited."]),
        ("5. Raising Concerns", ["Concerns about fraud or serious misconduct can be reported anonymously to ethics@acme-example.com."]),
        ("6. Attendance", ["Employees must mark attendance daily on the HR portal. Three late arrivals in a month are counted as "
                           "one half-day of leave."]),
        ("7. Social Media", ["Do not share confidential company information or client names on social media."]),
        ("8. Company Property", ["Company assets must be used for business purposes and returned when employment ends."]),
    ], break_every=2)


def data_privacy():
    (OUT / "data_privacy_policy.md").write_text("""# Data Privacy Policy

## Purpose

This policy describes how Acme Example Pvt Ltd keeps and protects personal data of employees and candidates.

## Retention Periods

| Record | Retention |
|---|---|
| HR personnel files | 7 years after the employee leaves |
| Payroll records | 8 years |
| CCTV footage | 30 days |
| Records of rejected job candidates | 1 year |
| Company email archives | 3 years |

## Data Breaches

A personal data breach must be reported to the Data Protection Officer (DPO) immediately. The company reports serious breaches to the authority within 72 hours.

## Employee Requests

An employee who asks to see their personal data gets a reply within 30 days. Requests are sent to dpo@acme-example.com.

## Sharing Data

Personal data is shared with third parties only when a contract with privacy terms is in place.
""", encoding="utf-8")


def vendor_sop():
    make_docx("vendor_onboarding_sop.docx", "Vendor Onboarding SOP", [
        ("h", "Steps"),
        ("num", "The requesting team submits a vendor request form to Procurement."),
        ("num", "Procurement completes due diligence (GST, PAN and bank account verification) within 5 working days."),
        ("num", "Legal reviews the contract terms."),
        ("num", "Finance creates the vendor code."),
        ("num", "The department head approves the vendor."),
        ("num", "Procurement issues the first purchase order."),
        ("h", "Approvals"),
        ("p", "Contracts above Rs 10,00,000 also need approval from the CFO."),
        ("h", "Re-verification"),
        ("p", "Vendors are re-verified every 2 years. A vendor that fails re-verification is blocked for new purchase orders."),
    ])


def benefits_guide():
    make_pdf("employee_benefits_guide.pdf", "Employee Benefits Guide", [
        ("1. Health Insurance", ["Group health insurance covers the employee, the spouse and up to 2 children. The sum insured is "
                                 "Rs 5,00,000 per family per year."]),
        ("2. Wellness Allowance", ["Employees can claim a wellness allowance of Rs 12,000 per year for gym or fitness memberships, "
                                   "against receipts."]),
        ("3. Referral Bonus", ["Employees receive a referral bonus of Rs 25,000 when a candidate they referred completes 6 months "
                               "in the company."]),
        ("4. Cab Facility", ["A drop-home cab is provided for employees whose shift ends after 9 PM."]),
        ("5. Benefits Summary", [[["Benefit", "Amount", "Frequency"],
                                  ["Health insurance", "Rs 5,00,000", "per family per year"],
                                  ["Wellness allowance", "Rs 12,000", "per year"],
                                  ["Referral bonus", "Rs 25,000", "per successful referral"]]]),
    ])


def leave_addendum():
    (OUT / "leave_policy_addendum.md").write_text("""# Leave Policy Addendum 2026

## Earned Leave Encashment

Up to 10 earned leave days can be encashed each year. Encashment requests are processed only in December.

## Compensatory Off

An employee who works on a public holiday earns a compensatory off. It must be taken within 60 days.

## Unauthorised Absence

Absence for 3 consecutive days without informing the manager is treated as unauthorised absence.

## Combining Leave

Casual leave cannot be combined with sick leave in a single application.
""", encoding="utf-8")


ALL = [travel_policy, expense_policy, it_security_policy, performance_policy, salary_matrix, code_of_conduct,
       data_privacy, vendor_sop, benefits_guide, leave_addendum]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for build in ALL:
        build()
    files = sorted(p.name for p in OUT.iterdir())
    print(f"Made {len(files)} evaluation documents in {OUT}:")
    for f in files:
        print("  ", f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
