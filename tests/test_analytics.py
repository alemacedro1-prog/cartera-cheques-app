from datetime import date

import pandas as pd

from utils.analytics import (
    apply_operational_scope,
    collection_calendar_summary,
    pending_collection,
    pending_for_month,
    receipt_summary,
    rejected_bank_summary,
)


def test_en_cartera_contains_every_movement_with_receipt_regardless_of_accreditation():
    portfolio = pd.DataFrame([
        {"ID": 1, "Estado recibo": "Tomado", "Estado calculado": "Acreditado", "Código estado": "AC"},
        {"ID": 2, "Estado recibo": "Tomado", "Estado calculado": "Pendiente de acreditación", "Código estado": "PS"},
        {"ID": 3, "Estado recibo": "Tomado", "Estado calculado": "Rescatado", "Código estado": "RE"},
        {"ID": 4, "Estado recibo": "Sin recibo asociado", "Estado calculado": "Pendiente", "Código estado": "PE"},
    ])

    result = apply_operational_scope(portfolio, "En cartera")

    assert result["ID"].tolist() == [1, 2, 3]


def test_pending_accreditation_scope_includes_ps_and_future_accreditation_dates():
    portfolio = pd.DataFrame([
        {"ID": 1, "Estado recibo": "Tomado", "Estado calculado": "Pendiente de acreditación", "Código estado": "PS"},
        {"ID": 2, "Estado recibo": "Tomado", "Estado calculado": "Pendiente de acreditación", "Código estado": "AC"},
        {"ID": 3, "Estado recibo": "Tomado", "Estado calculado": "Acreditado", "Código estado": "AC"},
    ])

    result = apply_operational_scope(portfolio, "Pend. acreditación")

    assert result["ID"].tolist() == [1, 2]


def test_pending_month_scope_uses_expected_collection_date_and_excludes_terminal_states():
    portfolio = pd.DataFrame([
        {"ID": 1, "Estado calculado": "Pendiente", "Fecha prevista de cobro": "05/08/2026", "Importe": 100},
        {"ID": 2, "Estado calculado": "Pendiente de acreditación", "Fecha prevista de cobro": "31/08/2026", "Importe": 200},
        {"ID": 3, "Estado calculado": "Pendiente", "Fecha prevista de cobro": "01/09/2026", "Importe": 300},
        {"ID": 4, "Estado calculado": "Acreditado", "Fecha prevista de cobro": "10/08/2026", "Importe": 400},
        {"ID": 5, "Estado calculado": "Rescatado", "Fecha prevista de cobro": "12/08/2026", "Importe": 500},
        {"ID": 6, "Estado calculado": "Rechazado", "Fecha prevista de cobro": "15/08/2026", "Importe": 600},
    ])

    assert pending_collection(portfolio)["ID"].tolist() == [1, 2, 3]
    assert pending_for_month(portfolio, date(2026, 8, 24))["ID"].tolist() == [1, 2]
    assert apply_operational_scope(portfolio, "Pendientes del mes", date(2026, 8, 24))["ID"].tolist() == [1, 2]


def test_collection_calendar_marks_dates_before_cutoff_as_already_elapsed():
    portfolio = pd.DataFrame([
        {"Estado calculado": "Pendiente", "Fecha prevista de cobro": "20/08/2026", "Cliente": "A", "Importe": 100},
        {"Estado calculado": "Pendiente", "Fecha prevista de cobro": "28/08/2026", "Cliente": "B", "Importe": 200},
    ])

    summary = collection_calendar_summary(portfolio, date(2026, 8, 24))

    assert summary["Situación"].tolist() == ["Fecha ya cumplida", "Próximo cobro"]
    assert summary["Importe"].sum() == 300


def test_receipt_summary_reports_counts_amounts_and_shares():
    portfolio = pd.DataFrame([
        {"Estado recibo": "Tomado", "Importe": 1000},
        {"Estado recibo": "Tomado", "Importe": 500},
        {"Estado recibo": "Sin recibo asociado", "Importe": 500},
    ])

    summary = receipt_summary(portfolio).set_index("Vínculo")

    assert summary.at["Con recibo", "Cantidad"] == 2
    assert summary.at["Con recibo", "Importe"] == 1500
    assert summary.at["Sin recibo", "Cantidad"] == 1
    assert summary.at["Sin recibo", "Importe"] == 500
    assert summary.at["Con recibo", "Participación cantidad"] == 2 / 3
    assert summary.at["Con recibo", "Participación importe"] == 0.75


def test_rejected_bank_summary_uses_only_macro_galicia_and_nacion():
    portfolio = pd.DataFrame([
        {"Estado calculado": "Rechazado", "Banco cheque": "285", "Cliente": "A", "Importe": 1000},
        {"Estado calculado": "Rechazado", "Banco cheque": "Banco Macro", "Cliente": "B", "Importe": 500},
        {"Estado calculado": "Rechazado", "Banco cheque": "007", "Cliente": "A", "Importe": 2000},
        {"Estado calculado": "Rechazado", "Banco cheque": "NACIÓN", "Cliente": "C", "Importe": 3000},
        {"Estado calculado": "Rechazado", "Banco cheque": "72", "Cliente": "D", "Importe": 9000},
        {"Estado calculado": "Pendiente", "Banco cheque": "285", "Cliente": "E", "Importe": 8000},
    ])

    summary = rejected_bank_summary(portfolio).set_index("Banco")

    assert summary.index.tolist() == ["Macro", "Galicia", "Nación"]
    assert summary.at["Macro", "Cantidad de rechazados"] == 2
    assert summary.at["Macro", "Importe rechazado"] == 1500
    assert summary.at["Macro", "Clientes afectados"] == 2
    assert summary.at["Galicia", "Importe rechazado"] == 2000
    assert summary.at["Nación", "Importe rechazado"] == 3000
    assert summary["Importe rechazado"].sum() == 6500
