from __future__ import annotations

import html
import io
from datetime import date

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether,
    LongTable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


NAVY = colors.HexColor("#17365D")
BLUE = colors.HexColor("#245A8D")
PALE_BLUE = colors.HexColor("#EDF3F8")
PALE_GREY = colors.HexColor("#F6F8FB")
TEXT = colors.HexColor("#23364A")
MUTED = colors.HexColor("#65758B")
RED = colors.HexColor("#C95651")


def _currency(value) -> str:
    parsed = pd.to_numeric(value, errors="coerce")
    numeric = 0.0 if pd.isna(parsed) else float(parsed)
    formatted = f"{numeric:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"$ {formatted}"


def _date_text(value) -> str:
    parsed = pd.to_datetime(value, errors="coerce")
    return "-" if pd.isna(parsed) else parsed.strftime("%d/%m/%Y")


def _text(value, fallback: str = "-") -> str:
    if value is None or pd.isna(value):
        return fallback
    cleaned = str(value).strip()
    return cleaned or fallback


def _paragraph(value, style: ParagraphStyle) -> Paragraph:
    return Paragraph(html.escape(_text(value)), style)


def _page_header_footer(canvas, document, cutoff: date) -> None:
    page_width, page_height = landscape(A4)
    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#D9E2EC"))
    canvas.setLineWidth(0.5)
    canvas.line(document.leftMargin, page_height - 11 * mm, page_width - document.rightMargin, page_height - 11 * mm)
    canvas.setFont("Helvetica-Bold", 8)
    canvas.setFillColor(NAVY)
    canvas.drawString(document.leftMargin, page_height - 8.2 * mm, "CARTERA DE CHEQUES")
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(MUTED)
    canvas.drawRightString(page_width - document.rightMargin, page_height - 8.2 * mm, f"Fecha de corte: {cutoff:%d/%m/%Y}")
    canvas.line(document.leftMargin, 11 * mm, page_width - document.rightMargin, 11 * mm)
    canvas.drawString(document.leftMargin, 7.2 * mm, "Reporte generado por Cartera de cheques")
    canvas.drawRightString(page_width - document.rightMargin, 7.2 * mm, f"Página {document.page}")
    canvas.restoreState()


def export_portfolio_pdf(portfolio: pd.DataFrame, cutoff: date) -> bytes:
    """Genera un PDF ejecutivo y detallado con todos los instrumentos recibidos."""
    output = io.BytesIO()
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ReportTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=23,
        leading=27,
        textColor=colors.white,
        alignment=TA_LEFT,
        spaceAfter=3 * mm,
    )
    subtitle_style = ParagraphStyle(
        "ReportSubtitle",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=13,
        textColor=colors.HexColor("#DDEAF6"),
    )
    section_style = ParagraphStyle(
        "Section",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=13,
        leading=16,
        textColor=NAVY,
        spaceBefore=2 * mm,
        spaceAfter=3 * mm,
    )
    body_style = ParagraphStyle(
        "Body",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=8.5,
        leading=12,
        textColor=TEXT,
    )
    metric_label_style = ParagraphStyle(
        "MetricLabel",
        parent=body_style,
        fontName="Helvetica-Bold",
        fontSize=7.5,
        leading=9,
        textColor=MUTED,
        alignment=TA_CENTER,
    )
    metric_value_style = ParagraphStyle(
        "MetricValue",
        parent=body_style,
        fontName="Helvetica-Bold",
        fontSize=14,
        leading=17,
        textColor=NAVY,
        alignment=TA_CENTER,
    )
    header_cell_style = ParagraphStyle(
        "HeaderCell",
        parent=body_style,
        fontName="Helvetica-Bold",
        fontSize=6.5,
        leading=7.5,
        textColor=colors.white,
        alignment=TA_CENTER,
    )
    cell_style = ParagraphStyle(
        "Cell",
        parent=body_style,
        fontSize=6.1,
        leading=7.4,
        textColor=TEXT,
        alignment=TA_LEFT,
    )
    cell_center_style = ParagraphStyle("CellCenter", parent=cell_style, alignment=TA_CENTER)
    cell_right_style = ParagraphStyle("CellRight", parent=cell_style, alignment=TA_RIGHT)

    document = SimpleDocTemplate(
        output,
        pagesize=landscape(A4),
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=22 * mm,
        bottomMargin=15 * mm,
        title=f"Cartera de cheques {cutoff:%Y-%m-%d}",
        author="Cartera de cheques",
        subject="Reporte completo de cartera",
    )

    portfolio = portfolio.copy()
    amounts = pd.to_numeric(portfolio.get("Importe", pd.Series(dtype=float)), errors="coerce").fillna(0)
    states = portfolio.get("Estado calculado", pd.Series(index=portfolio.index, dtype=object)).fillna("Sin estado")
    in_portfolio_states = ["Pendiente", "Pendiente de acreditación", "Vencido", "Vence hoy"]
    total_amount = float(amounts.sum())
    in_portfolio_amount = float(amounts[states.isin(in_portfolio_states)].sum())
    rejected_amount = float(amounts[states.eq("Rechazado")].sum())
    linked_count = int(portfolio.get("Estado recibo", pd.Series(index=portfolio.index, dtype=object)).eq("Tomado").sum())

    story = []
    hero = Table(
        [[
            Paragraph("Cartera de cheques", title_style),
            Paragraph(f"Reporte profesional completo<br/>Corte al {cutoff:%d/%m/%Y}", subtitle_style),
        ]],
        colWidths=[160 * mm, 88 * mm],
    )
    hero.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), NAVY),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8 * mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), 8 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8 * mm),
        ("BOX", (0, 0), (-1, -1), 0, NAVY),
    ]))
    story.extend([hero, Spacer(1, 7 * mm)])

    metric_data = [
        [
            Paragraph("INSTRUMENTOS", metric_label_style),
            Paragraph("IMPORTE TOTAL", metric_label_style),
            Paragraph("EN CARTERA", metric_label_style),
            Paragraph("RECHAZADOS", metric_label_style),
        ],
        [
            Paragraph(f"{len(portfolio):,}".replace(",", "."), metric_value_style),
            Paragraph(_currency(total_amount), metric_value_style),
            Paragraph(_currency(in_portfolio_amount), metric_value_style),
            Paragraph(_currency(rejected_amount), metric_value_style),
        ],
    ]
    metrics = Table(metric_data, colWidths=[62 * mm] * 4)
    metrics.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), PALE_BLUE),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#D3DEE9")),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D3DEE9")),
        ("TOPPADDING", (0, 0), (-1, 0), 4 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 1 * mm),
        ("TOPPADDING", (0, 1), (-1, 1), 1 * mm),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 4 * mm),
    ]))
    story.extend([metrics, Spacer(1, 7 * mm)])

    state_summary = (
        pd.DataFrame({"Estado": states, "Importe": amounts})
        .groupby("Estado", as_index=False)
        .agg(Instrumentos=("Importe", "size"), Importe=("Importe", "sum"))
        .sort_values("Importe", ascending=False)
    )
    state_rows = [[
        Paragraph("Estado", header_cell_style),
        Paragraph("Instrumentos", header_cell_style),
        Paragraph("Importe", header_cell_style),
        Paragraph("Participación", header_cell_style),
    ]]
    for record in state_summary.itertuples(index=False):
        share = float(record.Importe) / total_amount if total_amount else 0
        state_rows.append([
            _paragraph(record.Estado, body_style),
            Paragraph(f"{int(record.Instrumentos):,}".replace(",", "."), body_style),
            Paragraph(_currency(record.Importe), body_style),
            Paragraph(f"{share:.1%}".replace(".", ","), body_style),
        ])
    state_table = Table(state_rows, colWidths=[70 * mm, 35 * mm, 50 * mm, 35 * mm], repeatRows=1)
    state_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), BLUE),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, PALE_GREY]),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D9E2EC")),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#D9E2EC")),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 2.2 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2 * mm),
    ]))
    coverage = 100 * linked_count / len(portfolio) if len(portfolio) else 0
    summary_block = KeepTogether([
        Paragraph("Composición de la cartera", section_style),
        state_table,
        Spacer(1, 4 * mm),
        Paragraph(
            f"Cobertura de comprobantes: <b>{linked_count:,}</b> de <b>{len(portfolio):,}</b> instrumentos ({coverage:.1f}%). ".replace(",", ".")
            + "Los importes y estados surgen del archivo procesado para la fecha de corte indicada.",
            body_style,
        ),
    ])
    story.extend([summary_block, PageBreak()])

    story.extend([
        Paragraph("Detalle completo de instrumentos", section_style),
        Paragraph(
            "El listado incluye todos los movimientos de la cartera, ordenados por vencimiento y cliente. Los guiones indican datos no informados en el archivo fuente.",
            body_style,
        ),
        Spacer(1, 3 * mm),
    ])
    headers = ["#", "Cliente", "Tipo", "Cheque / eCheq", "Banco", "Importe", "Ingreso", "Vencimiento", "Estado", "Recibo", "Alertas"]
    detail_rows = [[Paragraph(header, header_cell_style) for header in headers]]
    ordered = portfolio.copy()
    ordered["_orden_vencimiento"] = pd.to_datetime(ordered.get("Fecha vencimiento"), errors="coerce")
    ordered = ordered.sort_values(["_orden_vencimiento", "Cliente"], na_position="last")
    for position, (_, row) in enumerate(ordered.iterrows(), start=1):
        detail_rows.append([
            Paragraph(str(position), cell_center_style),
            _paragraph(row.get("Cliente"), cell_style),
            _paragraph(row.get("Tipo"), cell_center_style),
            _paragraph(row.get("N° cheque / eCheq"), cell_center_style),
            _paragraph(row.get("Banco cheque"), cell_style),
            Paragraph(_currency(row.get("Importe")), cell_right_style),
            Paragraph(_date_text(row.get("Fecha ingreso / pago")), cell_center_style),
            Paragraph(_date_text(row.get("Fecha vencimiento")), cell_center_style),
            _paragraph(row.get("Estado calculado"), cell_style),
            _paragraph(row.get("Recibo relacionado"), cell_style),
            _paragraph(row.get("Alertas"), cell_style),
        ])
    detail_table = LongTable(
        detail_rows,
        colWidths=[8 * mm, 32 * mm, 13 * mm, 24 * mm, 23 * mm, 23 * mm, 19 * mm, 19 * mm, 27 * mm, 23 * mm, 31 * mm],
        repeatRows=1,
        splitByRow=1,
    )
    detail_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, PALE_GREY]),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D9E2EC")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 1.1 * mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 1.1 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), 1.4 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.4 * mm),
        ("TEXTCOLOR", (8, 1), (8, -1), TEXT),
    ]))
    story.append(detail_table)

    callback = lambda canvas, doc: _page_header_footer(canvas, doc, cutoff)
    document.build(story, onFirstPage=callback, onLaterPages=callback)
    return output.getvalue()
