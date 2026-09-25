"""Render the ECED-FLN glossary to a simple, branded PDF (green primary colour,
no logos). Inspired by the CALP glossary layout: a coloured cover, a running
header band, accent-coloured headings and clean body text.
"""
from xml.sax.saxutils import escape

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY, TA_LEFT
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, PageBreak,
                                KeepTogether)

GREEN = colors.HexColor('#007451')
GREEN_DARK = colors.HexColor('#00553b')
INK = colors.HexColor('#1a1a1a')
MUTED = colors.HexColor('#6b6b6b')
RULE = colors.HexColor('#d9e2de')

PAGE_W, PAGE_H = A4
MARGIN = 20 * mm
TITLE = 'ECED-FLN GLOSSARY'
SUBTITLE = 'Early Childhood Education & Development · Foundational Learning & Numeracy'


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
    }


def _cover(canvas, doc, meta):
    canvas.saveState()
    # full green cover
    canvas.setFillColor(GREEN)
    canvas.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
    # a darker angled band for a bit of depth (kept subtle)
    canvas.setFillColor(GREEN_DARK)
    p = canvas.beginPath()
    p.moveTo(0, PAGE_H * 0.34)
    p.lineTo(PAGE_W, PAGE_H * 0.42)
    p.lineTo(PAGE_W, PAGE_H * 0.30)
    p.lineTo(0, PAGE_H * 0.22)
    p.close()
    canvas.drawPath(p, fill=1, stroke=0)

    canvas.setFillColor(colors.white)
    canvas.setFont('Helvetica-Bold', 40)
    canvas.drawString(MARGIN, PAGE_H * 0.60, 'ECED-FLN')
    canvas.drawString(MARGIN, PAGE_H * 0.60 - 44, 'GLOSSARY')
    canvas.setFont('Helvetica', 12)
    canvas.drawString(MARGIN, PAGE_H * 0.60 - 74,
                      'Early Childhood Education & Development')
    canvas.drawString(MARGIN, PAGE_H * 0.60 - 92,
                      'Foundational Learning & Numeracy')
    # footer meta on cover
    canvas.setFont('Helvetica', 10)
    canvas.setFillColor(colors.white)
    canvas.drawString(MARGIN, 28 * mm,
                      f"{meta.get('count', 0)} terms  ·  Updated {meta.get('date', '')}")
    canvas.drawString(MARGIN, 28 * mm - 15, meta.get('site', ''))
    canvas.restoreState()


def _header(canvas, doc):
    canvas.saveState()
    band_h = 16 * mm
    canvas.setFillColor(GREEN)
    canvas.rect(0, PAGE_H - band_h, PAGE_W, band_h, fill=1, stroke=0)
    canvas.setFillColor(colors.white)
    canvas.setFont('Helvetica-Bold', 10)
    canvas.drawString(MARGIN, PAGE_H - band_h + 5.5 * mm, 'ECED-FLN GLOSSARY')
    canvas.setFont('Helvetica', 8)
    canvas.drawRightString(PAGE_W - MARGIN, PAGE_H - band_h + 5.5 * mm,
                           'Early Childhood Education & Development · Foundational Learning & Numeracy')
    # page number
    canvas.setFillColor(MUTED)
    canvas.setFont('Helvetica', 8)
    canvas.drawCentredString(PAGE_W / 2, 12 * mm, str(doc.page))
    canvas.restoreState()


def build_glossary_pdf(path, terms, meta):
    """terms: list of dicts {term, definition, aliases:[...]}. meta: {count,date,site}."""
    st = _styles()
    doc = SimpleDocTemplate(path, pagesize=A4,
                            leftMargin=MARGIN, rightMargin=MARGIN,
                            topMargin=22 * mm, bottomMargin=18 * mm,
                            title='ECED-FLN Glossary', author='AU ECED-FLN Cluster')

    story = [PageBreak()]  # page 1 is the drawn cover
    story.append(Paragraph('About this glossary', st['h']))
    story.append(Paragraph(
        'This glossary defines key terms used across Early Childhood Education &amp; '
        'Development (ECED) and Foundational Learning &amp; Numeracy (FLN) initiatives '
        'submitted to the AU ECED-FLN Cluster platform. Definitions are grounded in how '
        'the terms are used in practice across initiatives from many African countries. '
        'This document is generated automatically from the live glossary and reflects its '
        'most recent revisions.', st['body']))
    story.append(Spacer(1, 4))

    current_letter = None
    for t in terms:
        name = (t.get('term') or '').strip()
        if not name:
            continue
        letter = name[0].upper()
        if not letter.isalpha():
            letter = '#'
        if letter != current_letter:
            current_letter = letter
            story.append(Spacer(1, 2))
            story.append(Paragraph(letter, st['letter']))

        block = [Paragraph(escape(name), st['term']),
                 Paragraph(escape((t.get('definition') or '').strip() or '—'), st['def'])]
        aliases = [a for a in (t.get('aliases') or []) if a and a.strip().lower() != name.lower()]
        if aliases:
            block.append(Paragraph('Also known as: ' + escape(', '.join(aliases)), st['alias']))
        story.append(KeepTogether(block))

    def on_first(c, d):
        _cover(c, d, meta)

    doc.build(story, onFirstPage=on_first, onLaterPages=lambda c, d: _header(c, d))
    return path
