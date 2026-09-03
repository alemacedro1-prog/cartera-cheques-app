from datetime import date

import pandas as pd
import pytest

from utils.analytics import (
    apply_operational_scope,
    collection_calendar_summary,
    deposit_bank_group,
    rejected_bank_detail,
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


def test_rejected_bank_summary_uses_deposit_bank_not_issuer_and_preserves_exceptions():
    portfolio = pd.DataFrame([
        {"Estado calculado": "Rechazado", "Banco depósito": "285", "Banco cheque": "ICBC", "Cliente": "A", "Importe": 1000},
        {"Estado calculado": "Rechazado", "Banco depósito": "Banco Macro", "Banco cheque": "CREDICOOP", "Cliente": "B", "Importe": 500},
        {"Estado calculado": "Rechazado", "Banco depósito": "007", "Banco cheque": "SANTA FE", "Cliente": "A", "Importe": 2000},
        {"Estado calculado": "Rechazado", "Banco depósito": "NACIÓN", "Banco cheque": "285", "Cliente": "C", "Importe": 3000},
        {"Estado calculado": "Rechazado", "Banco depósito": "ICBC", "Banco cheque": "285", "Cliente": "D", "Importe": 9000},
        {"Estado calculado": "Rechazado", "Banco depósito": None, "Banco cheque": "Galicia", "Cliente": "F", "Importe": 400},
        {"Estado calculado": "Pendiente", "Banco depósito": "285", "Cliente": "E", "Importe": 8000},
        {"Estado calculado": "Acreditado", "Banco depósito": "285", "Cliente": "G", "Importe": 750},
        {"Estado calculado": "Rescatado", "Banco depósito": "285", "Cliente": "H", "Importe": 650},
    ])

    summary = rejected_bank_summary(portfolio).set_index("Banco")

    assert summary.index.tolist() == ["Macro", "Galicia", "Nación", "Otros bancos de depósito", "Sin banco de depósito"]
    assert summary.at["Macro", "Cantidad de rechazados"] == 2
    assert summary.at["Macro", "Importe rechazado"] == 1500
    assert summary.at["Macro", "Clientes afectados"] == 2
    assert summary.at["Galicia", "Importe rechazado"] == 2000
    assert summary.at["Nación", "Importe rechazado"] == 3000
    assert summary.at["Otros bancos de depósito", "Importe rechazado"] == 9000
    assert summary.at["Sin banco de depósito", "Importe rechazado"] == 400
    assert summary["Importe rechazado"].sum() == 15900
    assert summary["Cantidad de rechazados"].sum() == 6


@pytest.mark.parametrize(("value", "expected"), [
    ("Banco Macro S.A.", "Macro"), (285.0, "Macro"), ("007", "Galicia"),
    ("Banco de la Nación Argentina", "Nación"), ("011", "Nación"),
    ("ICBC", "Otros bancos de depósito"), ("CREDICOOP", "Otros bancos de depósito"),
    ("SANTA FE", "Otros bancos de depósito"), (None, "Sin banco de depósito"),
    ("", "Sin banco de depósito"), (0, "Sin banco de depósito"), (float("nan"), "Sin banco de depósito"),
])
def test_deposit_bank_normalization(value, expected):
    assert deposit_bank_group(value) == expected


def test_missing_deposit_bank_never_falls_back_to_issuer_and_keeps_audit_fields():
    portfolio = pd.DataFrame([{
        "Estado calculado": "Rechazado", "Banco cheque": "Macro", "Importe": 100,
        "Cliente": "A", "Filas originales del CONRENPF": "4, 8",
    }])
    before = portfolio.copy(deep=True)
    detail = rejected_bank_detail(portfolio)
    assert detail.iloc[0]["Banco de depósito agrupado"] == "Sin banco de depósito"
    assert detail.iloc[0]["Filas originales del CONRENPF"] == "4, 8"
    summary = rejected_bank_summary(portfolio).set_index("Banco")
    assert summary.at["Macro", "Importe rechazado"] == 0
    assert summary.at["Sin banco de depósito", "Importe rechazado"] == 100
    pd.testing.assert_frame_equal(before, portfolio)


def test_three_deposit_banks_have_no_other_category_when_all_are_identified():
    portfolio = pd.DataFrame([{"Estado calculado": "Rechazado", "Banco depósito": "Galicia", "Importe": 200}])
    assert rejected_bank_summary(portfolio)["Banco"].tolist() == ["Macro", "Galicia", "Nación"]
