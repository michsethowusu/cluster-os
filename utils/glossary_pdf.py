"""Render the AU ECED-FLN glossary to a simple, branded PDF (green primary colour).

Layout (inspired by the CALP glossary): a cover with a white band carrying the AU
logo in colour over a green field, a clickable Contents page (jump to any term),
a running header band, and A-Z term sections with definitions and aliases.
"""
import os
from xml.sax.saxutils import escape

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, PageBreak,
                                KeepTogether, Table, TableStyle)

GREEN = colors.HexColor('#007451')
GREEN_DARK = colors.HexColor('#00553b')
INK = colors.HexColor('#1a1a1a')
MUTED = colors.HexColor('#6b6b6b')

PAGE_W, PAGE_H = A4
MARGIN = 20 * mm
PRODUCER = 'Produced by the African Union ECED-FLN Cluster'
CLUSTER_URL = 'https://www.ecedflncluster-au.org/'


def _styles():
    return {
        'letter': ParagraphStyle('letter', fontName='Helvetica-Bold', fontSize=15,
                                 textColor=GREEN, spaceBefore=10, spaceAfter=4, leading=18),
        'term': ParagraphStyle('term', fontName='Helvetica-Bold', fontSize=11,
                               textColor=GREEN_DARK, spaceBefore=8, spaceAfter=1, leading=14),
        'def': ParagraphStyle('def', fontName='Helvetica', fontSize=9.5, textColor=INK,
                              alignment=TA_JUSTIFY, leading=13),
        'alias': ParagraphStyle('alias', fontName='Helvetica-Oblique', fontSize=8,
                               textColor=MUTED, spaceBefore=1, leading=11),
        'h': ParagraphStyle('h', fontName='Helvetica-Bold', fontSize=13, textColor=GREEN,
                            spaceBefore=6, spaceAfter=4, leading=16),
        'body': ParagraphStyle('body', fontName='Helvetica', fontSize=10, textColor=INK,
                              alignment=TA_JUSTIFY, leading=14, spaceAfter=6),
        'toc': ParagraphStyle('toc', fontName='Helvetica', fontSize=8.5, textColor=INK,
                             leading=12),
        'nav': ParagraphStyle('nav', fontName='Helvetica-Bold', fontSize=11, textColor=GREEN,
                             leading=18),
    }


def _cover(canvas, doc, meta):
    canvas.saveState()
    # A slim white band at the top — just tall enough for the logo.
    logo_h = 46
    pad = 11
    band_h = logo_h + pad * 2
    green_top = PAGE_H - band_h
    # white top band is the page background; fill the rest green
    canvas.setFillColor(GREEN)
    canvas.rect(0, 0, PAGE_W, green_top, fill=1, stroke=0)
    # subtle darker angled band for depth
    canvas.setFillColor(GREEN_DARK)
    p = canvas.beginPath()
    p.moveTo(0, green_top * 0.34)
    p.lineTo(PAGE_W, green_top * 0.42)
    p.lineTo(PAGE_W, green_top * 0.30)
    p.lineTo(0, green_top * 0.22)
    p.close()
    canvas.drawPath(p, fill=1, stroke=0)

    # AU logo (colour), left-aligned in the white band
    logo = meta.get('logo')
    if logo and os.path.exists(logo):
        try:
            iw, ih = ImageReader(logo).getSize()
            w = logo_h * (iw / ih)
            canvas.drawImage(logo, MARGIN, green_top + pad, width=w, height=logo_h,
                             mask='auto', preserveAspectRatio=True)
        except Exception:
            pass

    # title
    canvas.setFillColor(colors.white)
    canvas.setFont('Helvetica-Bold', 38)
    canvas.drawString(MARGIN, green_top * 0.60, 'AU ECED-FLN')
    canvas.drawString(MARGIN, green_top * 0.60 - 42, 'GLOSSARY')
    canvas.setFont('Helvetica', 12)
    canvas.drawString(MARGIN, green_top * 0.60 - 72, 'Early Childhood Education & Development')
    canvas.drawString(MARGIN, green_top * 0.60 - 90, 'Foundational Learning & Numeracy')

    # footer: producer + URL + counts
    canvas.setFillColor(colors.white)
    canvas.setFont('Helvetica-Bold', 11)
    canvas.drawString(MARGIN, 34 * mm, PRODUCER)
    canvas.setFont('Helvetica', 10)
    canvas.drawString(MARGIN, 34 * mm - 16, CLUSTER_URL)
    canvas.setFont('Helvetica', 9)
    canvas.drawString(MARGIN, 34 * mm - 34,
                      f"{meta.get('count', 0)} terms  ·  Updated {meta.get('date', '')}")
    canvas.restoreState()


def _header(canvas, doc):
    canvas.saveState()
    band_h = 16 * mm
    canvas.setFillColor(GREEN)
    canvas.rect(0, PAGE_H - band_h, PAGE_W, band_h, fill=1, stroke=0)
    canvas.setFillColor(colors.white)
    canvas.setFont('Helvetica-Bold', 10)
    canvas.drawString(MARGIN, PAGE_H - band_h + 5.5 * mm, 'AU ECED-FLN GLOSSARY')
    canvas.setFont('Helvetica', 8)
    canvas.drawRightString(PAGE_W - MARGIN, PAGE_H - band_h + 5.5 * mm,
                           'African Union ECED-FLN Cluster')
    canvas.setFillColor(MUTED)
    canvas.setFont('Helvetica', 8)
    canvas.drawCentredString(PAGE_W / 2, 12 * mm, str(doc.page))
    canvas.restoreState()


def build_glossary_pdf(path, terms, meta):
    """terms: list of dicts {term, definition, aliases:[...]}. meta: {count,date,logo}."""
    st = _styles()
    doc = SimpleDocTemplate(path, pagesize=A4,
                            leftMargin=MARGIN, rightMargin=MARGIN,
                            topMargin=22 * mm, bottomMargin=18 * mm,
                            title='AU ECED-FLN Glossary',
                            author='African Union ECED-FLN Cluster')

    # index terms with stable anchors, keep only those with a name
    indexed = []
    for t in terms:
        name = (t.get('term') or '').strip()
        if name:
            indexed.append((len(indexed), name, t))
    letters = []
    for _, name, _t in indexed:
        L = name[0].upper()
        L = L if L.isalpha() else '#'
        if L not in letters:
            letters.append(L)

    story = [PageBreak()]  # page 1 is the drawn cover

    # ---- Contents ----
    story.append(Paragraph('Contents', st['h']))
    story.append(Paragraph(
        'Jump to a letter, or click any term below to go straight to its entry.', st['body']))
    nav = '  '.join(f'<a href="#L_{L}" color="#007451">{L}</a>' for L in letters)
    story.append(Paragraph(nav, st['nav']))
    story.append(Spacer(1, 8))

    # two-column clickable index of every term
    links = [Paragraph(f'<a href="#t{i}" color="#00553b">{escape(name)}</a>', st['toc'])
             for i, name, _t in indexed]
    rows = []
    for k in range(0, len(links), 2):
        rows.append([links[k], links[k + 1] if k + 1 < len(links) else ''])
    if rows:
        col = (PAGE_W - 2 * MARGIN) / 2
        tbl = Table(rows, colWidths=[col, col])
        tbl.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('TOPPADDING', (0, 0), (-1, -1), 1),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
            ('LEFTPADDING', (0, 0), (-1, -1), 0),
            ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ]))
        story.append(tbl)

    story.append(PageBreak())

    # ---- About + entries ----
    story.append(Paragraph('About this glossary', st['h']))
    story.append(Paragraph(
        'This glossary defines key terms used across Early Childhood Education &amp; '
        'Development (ECED) and Foundational Learning &amp; Numeracy (FLN) initiatives '
        'submitted to the African Union ECED-FLN Cluster platform. Definitions are grounded '
        'in how the terms are used in practice across initiatives from many African countries. '
        'This document is generated automatically and reflects the glossary\'s most recent '
        'revisions.', st['body']))
    story.append(Spacer(1, 4))

    current = None
    for i, name, t in indexed:
        L = name[0].upper()
        L = L if L.isalpha() else '#'
        if L != current:
            current = L
            story.append(Spacer(1, 2))
            story.append(Paragraph(f'<a name="L_{L}"/>{L}', st['letter']))
        block = [Paragraph(f'<a name="t{i}"/>{escape(name)}', st['term']),
                 Paragraph(escape((t.get('definition') or '').strip() or '—'), st['def'])]
        aliases = [a for a in (t.get('aliases') or []) if a and a.strip().lower() != name.lower()]
        if aliases:
            block.append(Paragraph('Also known as: ' + escape(', '.join(aliases)), st['alias']))
        story.append(KeepTogether(block))

    doc.build(story, onFirstPage=lambda c, d: _cover(c, d, meta),
              onLaterPages=lambda c, d: _header(c, d))
    return path
