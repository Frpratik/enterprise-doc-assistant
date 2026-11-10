"""
Generate FAKE HR documents of every type (+ some bad files) for testing.
We never use real company data.

Run:  python scripts/make_sample_data.py
Output: data/samples/good/  and  data/samples/bad/
"""
import shutil
import tempfile
from pathlib import Path

import pandas as pd
from docx import Document
from docx.shared import Inches
from PIL import Image, ImageDraw, ImageFont
from pptx import Presentation
from pptx.util import Inches as PInches
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Image as RLImage
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

ROOT = Path(__file__).resolve().parent.parent
GOOD = ROOT / "data" / "samples" / "good"
BAD = ROOT / "data" / "samples" / "bad"
TMP = Path(tempfile.mkdtemp())   # helper images live in the system temp folder (outside the project)
styles = getSampleStyleSheet()


# ---------- helpers ----------
def text_image(lines, path, width=1400, font_size=34, line_gap=18, rotate=0.0):
    """Draw black text on a white image (used to fake scans / photos)."""
    font = ImageFont.load_default(size=font_size)
    height = 80 + len(lines) * (font_size + line_gap)
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    y = 40
    for line in lines:
        draw.text((50, y), line, fill="black", font=font)
        y += font_size + line_gap
    if rotate:
        img = img.rotate(rotate, expand=True, fillcolor="white")  # slightly tilted, like a real scan
    img.save(path)
    return path


def pdf_table(rows):
    t = Table(rows)
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
    ]))
    return t


# ---------- GOOD files ----------
def digital_pdf():
    """leave_policy.pdf: normal digital PDF with headings, paragraphs, a table, 2 pages."""
    doc = SimpleDocTemplate(str(GOOD / "leave_policy.pdf"), pagesize=A4, title="Leave Policy 2026", author="HR Team")
    s = [
        Paragraph("Leave Policy 2026", styles["Title"]),
        Paragraph("1. Sick Leave", styles["Heading2"]),
        Paragraph("Employees get 12 sick leaves per year. A medical certificate is required "
                  "for sick leave longer than 2 consecutive days.", styles["BodyText"]),
        Paragraph("2. Casual Leave", styles["Heading2"]),
        Paragraph("Employees get 8 casual leaves per year. Casual leave must be applied at least "
                  "1 day in advance through the HR portal.", styles["BodyText"]),
        Spacer(1, 12),
        Paragraph("Leave Summary", styles["Heading3"]),
        pdf_table([["Leave Type", "Days per Year", "Carry Forward"],
                   ["Sick Leave", "12", "No"],
                   ["Casual Leave", "8", "No"],
                   ["Earned Leave", "18", "Yes, up to 30 days"]]),
        PageBreak(),
        Paragraph("3. Maternity and Paternity Leave", styles["Heading2"]),
        Paragraph("Maternity leave is 26 weeks. Paternity leave is 10 working days, to be taken "
                  "within 3 months of the child's birth.", styles["BodyText"]),
        Paragraph("4. Notice Period", styles["Heading2"]),
        Paragraph("The notice period for all permanent employees is 60 days.", styles["BodyText"]),
    ]
    doc.build(s)


def scanned_pdf():
    """scanned_code_of_conduct.pdf: pages are IMAGES only (no text layer) -> needs OCR."""
    p1 = text_image([
        "CODE OF CONDUCT",
        "",
        "1. Treat every colleague with respect.",
        "2. Office hours are 9:30 AM to 6:30 PM.",
        "3. Report any harassment to hr@acme-example.com.",
        "4. Company laptops must be locked when unattended.",
    ], TMP / "scan_p1.png", rotate=0.6)
    p2 = text_image([
        "DRESS CODE",
        "",
        "Business casual from Monday to Thursday.",
        "Friday is casual dress day.",
        "",
        "Approved by: HR Head, Acme Example Pvt Ltd",
    ], TMP / "scan_p2.png", rotate=-0.4)
    images = [Image.open(p).convert("RGB") for p in (p1, p2)]
    images[0].save(GOOD / "scanned_code_of_conduct.pdf", save_all=True, append_images=images[1:], resolution=150)


def mixed_pdf():
    """mixed_quarterly_report.pdf: page 1 digital text + table + embedded image with text,
    page 2 is a full scanned image."""
    banner = text_image(["Employee Satisfaction Score: 4.2 / 5"], TMP / "banner.png", width=1100, font_size=40)
    scan = text_image([
        "ANNEXURE A - SIGNED APPROVAL",
        "",
        "Budget for Q4 training approved: Rs 5,00,000",
        "Signed: Finance Head",
    ], TMP / "annex.png", rotate=0.5)

    tmp_pdf = TMP / "mixed_part1.pdf"
    doc = SimpleDocTemplate(str(tmp_pdf), pagesize=A4, title="Quarterly HR Report Q3 2026")
    doc.build([
        Paragraph("Quarterly HR Report - Q3 2026", styles["Title"]),
        Paragraph("Headcount", styles["Heading2"]),
        Paragraph("Total headcount at the end of Q3 was 412 employees, up from 389 in Q2.", styles["BodyText"]),
        pdf_table([["Department", "Headcount", "Attrition %"],
                   ["Engineering", "210", "6.5"],
                   ["Sales", "95", "11.2"],
                   ["HR", "18", "3.0"]]),
        Spacer(1, 12),
        RLImage(str(banner), width=400, height=60),
        PageBreak(),
        RLImage(str(scan), width=450, height=200),   # page 2 = image only (like a scan)
    ])
    shutil.move(str(tmp_pdf), GOOD / "mixed_quarterly_report.pdf")


def password_pdf():
    """password_protected.pdf: encrypted PDF -> should fail with a clear reason."""
    from reportlab.lib import pdfencrypt
    enc = pdfencrypt.StandardEncryption("secret123", canPrint=0)
    doc = SimpleDocTemplate(str(BAD / "password_protected.pdf"), pagesize=A4, encrypt=enc)
    doc.build([Paragraph("Confidential salary bands.", styles["BodyText"])])


def word_doc():
    """employee_handbook.docx: headings, paragraphs, bullet list, table, embedded image with text."""
    img = text_image(["Helpdesk: ext 4455"], TMP / "helpdesk.png", width=700, font_size=40)
    d = Document()
    d.core_properties.title = "Employee Handbook"
    d.core_properties.author = "HR Team"
    d.add_heading("Employee Handbook", level=1)
    d.add_heading("Working Hours", level=2)
    d.add_paragraph("Standard working hours are 9:30 AM to 6:30 PM, Monday to Friday.")
    d.add_heading("Benefits", level=2)
    for b in ["Health insurance for employee and family", "Annual learning budget of Rs 25,000",
              "Free lunch on Fridays"]:
        d.add_paragraph(b, style="List Bullet")
    d.add_heading("Reimbursement Limits", level=2)
    t = d.add_table(rows=1, cols=2)
    t.style = "Table Grid"
    t.rows[0].cells[0].text, t.rows[0].cells[1].text = "Expense", "Monthly Limit (Rs)"
    for exp, lim in [("Internet", "1,000"), ("Mobile", "800"), ("Travel", "5,000")]:
        r = t.add_row().cells
        r[0].text, r[1].text = exp, lim
    d.add_heading("IT Support", level=2)
    d.add_picture(str(img), width=Inches(3))
    d.save(GOOD / "employee_handbook.docx")


def ppt_deck():
    """onboarding_deck.pptx: title slide, bullets, a table, an image with text, speaker notes."""
    img = text_image(["Welcome to Acme Example!"], TMP / "welcome.png", width=900, font_size=44)
    prs = Presentation()
    s1 = prs.slides.add_slide(prs.slide_layouts[0])
    s1.shapes.title.text = "New Joiner Onboarding"
    s1.placeholders[1].text = "HR Team - 2026"
    s1.notes_slide.notes_text_frame.text = "Speaker note: greet everyone and share the agenda."

    s2 = prs.slides.add_slide(prs.slide_layouts[1])
    s2.shapes.title.text = "First Week Checklist"
    tf = s2.placeholders[1].text_frame
    tf.text = "Collect laptop from IT"
    for item in ["Complete payroll form", "Meet your buddy", "Finish security training"]:
        tf.add_paragraph().text = item

    s3 = prs.slides.add_slide(prs.slide_layouts[5])
    s3.shapes.title.text = "Key Contacts"
    rows = [("Team", "Email"), ("HR", "hr@acme-example.com"), ("IT", "it@acme-example.com")]
    table = s3.shapes.add_table(len(rows), 2, PInches(1), PInches(2), PInches(7), PInches(1.5)).table
    for i, (a, b) in enumerate(rows):
        table.cell(i, 0).text, table.cell(i, 1).text = a, b

    s4 = prs.slides.add_slide(prs.slide_layouts[5])
    s4.shapes.title.text = "Welcome"
    s4.shapes.add_picture(str(img), PInches(1), PInches(2), width=PInches(7))
    prs.save(GOOD / "onboarding_deck.pptx")


def spreadsheets():
    """salary_structure.xlsx (2 sheets) + holidays_2026.csv"""
    with pd.ExcelWriter(GOOD / "salary_structure.xlsx") as w:
        pd.DataFrame({
            "Grade": ["L1", "L2", "L3"],
            "Min CTC (LPA)": [4, 8, 15],
            "Max CTC (LPA)": [7, 14, 25],
        }).to_excel(w, sheet_name="Salary Bands", index=False)
        pd.DataFrame({
            "Component": ["Basic", "HRA", "Special Allowance"],
            "Percent of CTC": [40, 20, 40],
        }).to_excel(w, sheet_name="Components", index=False)
    pd.DataFrame({
        "Date": ["2026-01-26", "2026-08-15", "2026-10-20", "2026-11-08"],
        "Holiday": ["Republic Day", "Independence Day", "Dussehra", "Diwali"],
    }).to_csv(GOOD / "holidays_2026.csv", index=False)


def image_file():
    """notice_board_photo.png: a 'photo' of a notice -> OCR."""
    text_image([
        "NOTICE",
        "Fire drill on 15 November 2026 at 11:00 AM.",
        "Please assemble at the main parking area.",
    ], GOOD / "notice_board_photo.png", rotate=1.0)


def text_files():
    (GOOD / "it_policy.txt").write_text(
        "IT POLICY\n\nPasswords must be changed every 90 days.\n"
        "Do not install unapproved software on company laptops.\n", encoding="utf-8")
    (GOOD / "remote_work.md").write_text(
        "# Remote Work Policy\n\n## Eligibility\nEmployees can work from home 2 days a week.\n\n"
        "## Equipment\n| Item | Provided |\n|---|---|\n| Laptop | Yes |\n| Monitor | On request |\n",
        encoding="utf-8")
    (GOOD / "hr_faq.html").write_text("""<html><head><title>HR FAQ</title>
<script>console.log('tracking');</script></head>
<body><nav>Home | About | Contact</nav>
<h1>HR FAQ</h1>
<h2>How do I apply for leave?</h2><p>Use the HR portal under Leave &gt; Apply.</p>
<h2>When is salary credited?</h2><p>Salary is credited on the last working day of the month.</p>
<table><tr><th>Query</th><th>Contact</th></tr><tr><td>Payroll</td><td>payroll@acme-example.com</td></tr></table>
<footer>Copyright Acme Example</footer></body></html>""", encoding="utf-8")


# ---------- BAD files ----------
def bad_files():
    # 1. an "exe" pretending to be a PDF (Windows .exe files start with the bytes 'MZ')
    (BAD / "salary_details.pdf").write_bytes(b"MZ\x90\x00" + b"\x00" * 200)
    # 2. corrupted PDF (header only, rest is junk)
    (BAD / "corrupted_report.pdf").write_bytes(b"%PDF-1.4\n" + b"\x00garbage\xff" * 50)
    # 3. empty file
    (BAD / "empty_notes.txt").write_bytes(b"")
    # 4. not allowed type
    (BAD / "setup_tool.exe").write_bytes(b"MZ\x90\x00" + b"\x00" * 100)
    # 5. duplicate of a good file (same bytes, different name)
    shutil.copy(GOOD / "leave_policy.pdf", BAD / "leave_policy_copy.pdf")
    # 6. password protected PDF
    password_pdf()


if __name__ == "__main__":
    for d in (GOOD, BAD):
        d.mkdir(parents=True, exist_ok=True)
    digital_pdf()
    scanned_pdf()
    mixed_pdf()
    word_doc()
    ppt_deck()
    spreadsheets()
    image_file()
    text_files()
    bad_files()
    shutil.rmtree(TMP, ignore_errors=True)
    print("Sample files created:")
    for folder in (GOOD, BAD):
        for f in sorted(folder.iterdir()):
            print(f"  {folder.name:5} | {f.name:32} | {f.stat().st_size:>8,} bytes")
