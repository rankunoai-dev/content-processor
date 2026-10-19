"""Programmatic DOCX, XLSX and PPTX builders shared by the Office tests."""

from __future__ import annotations

import io
import zipfile
from typing import Any

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from openpyxl import Workbook
from pptx import Presentation
from pptx.util import Inches


def _bytes(obj: Any) -> bytes:
    buf = io.BytesIO()
    obj.save(buf)
    return buf.getvalue()


def docx_full() -> bytes:
    """Heading, paragraphs, a table, hidden text, a tracked deletion, a comment, a header."""
    doc = Document()
    doc.add_heading("Quarterly review", 1)
    doc.add_paragraph("Revenue grew 12% in Q3.")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Region"
    table.cell(0, 1).text = "Sales"
    table.cell(1, 0).text = "North"
    table.cell(1, 1).text = "40"

    hidden_para = doc.add_paragraph()
    run = hidden_para.add_run("secret internal note")
    run.font.hidden = True

    deleted_para = doc.add_paragraph("Price is ")
    deleted = OxmlElement("w:del")
    deleted.set(qn("w:id"), "1")
    deleted.set(qn("w:author"), "reviewer")
    del_run = OxmlElement("w:r")
    del_text = OxmlElement("w:delText")
    del_text.text = "10 dollars"
    del_run.append(del_text)
    deleted.append(del_run)
    deleted_para._p.append(deleted)
    ins = OxmlElement("w:ins")
    ins.set(qn("w:id"), "2")
    ins.set(qn("w:author"), "reviewer")
    ins_run = OxmlElement("w:r")
    ins_text = OxmlElement("w:t")
    ins_text.text = "12 dollars"
    ins_run.append(ins_text)
    ins.append(ins_run)
    deleted_para._p.append(ins)

    commented = doc.add_paragraph("Clause 9 needs legal review.")
    doc.add_comment(commented.runs, text="check with legal", author="reviewer", initials="R")

    doc.sections[0].header.paragraphs[0].text = "ACME CONFIDENTIAL"
    return _bytes(doc)


def xlsx_full() -> bytes:
    """Visible sheet with a formula whose value is not cached, plus a hidden sheet."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Costs"
    ws.append(["item", "usd"])
    ws.append(["hosting", 120])
    ws.append(["domain", 15])
    ws.append(["total", "=B2+B3"])
    hidden = wb.create_sheet("Scratch")
    hidden.append(["draft only"])
    hidden.sheet_state = "hidden"
    return _bytes(wb)


def pptx_full() -> bytes:
    """Title and body, a table, speaker notes, a hidden slide and a picture with alt text."""
    prs = Presentation()
    s1 = prs.slides.add_slide(prs.slide_layouts[1])
    s1.shapes.title.text = "Roadmap"
    s1.placeholders[1].text = "Launch in October"
    s1.notes_slide.notes_text_frame.text = "Mention the pilot customers."

    s2 = prs.slides.add_slide(prs.slide_layouts[5])
    s2.shapes.title.text = "Budget"
    rows = [["Item", "USD"], ["Hosting", "120"]]
    table = s2.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(6), Inches(1)).table
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            table.cell(r, c).text = value

    s3 = prs.slides.add_slide(prs.slide_layouts[1])
    s3.shapes.title.text = "Appendix"
    s3.placeholders[1].text = "Hidden detail"
    s3._element.set("show", "0")
    return _bytes(prs)


def zip_bomb_like() -> bytes:
    """A valid zip whose member expands far beyond its compressed size."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", b"\x00" * (50 * 1024 * 1024))
    return buf.getvalue()
