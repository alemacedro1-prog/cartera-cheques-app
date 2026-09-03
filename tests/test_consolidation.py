from datetime import date
from io import BytesIO

import pandas as pd
import pytest

from utils.analytics import pending_collection, rejected_bank_summary
from utils.consolidation import SOURCE_ROWS, rejected_then_accredited, select_source_rows
from utils.movements import (
    MANUAL_ORIGIN,
    build_movement_control,
    export_movements_excel,
    manual_accredited_movements,
    pending_movements_detail,
    receipt_reconciliation,
)
from utils.portfolio import build_portfolio, export_excel

CUTOFF = date(2026, 9, 3)


def row(state="PS", number="12345", amount=1000.25, **changes):
    result = {
        "MCR-Medio de pago": "CPD",
        "MCR-Estado instr.": state,
        "MCR-Número de cheque": number,
        "MCR-Importe instr.": amount,
        "MCR-Fecha pago": "01/09/2026",
        "MCR-Fecha vencim.": "10/09/2026",
        "MCR-Nombre cliente": "Cliente de prueba",
        "MCR-Banco": "285",
        "Observación": "Recibo 756699",
    }
    result.update(changes)
    return result


def raw_frame(*rows):
    frame = pd.DataFrame(rows)
    frame["Fila fuente"] = range(4, len(rows) + 4)
    return frame


@pytest.mark.parametrize("previous", ["RC", "PS"])
@pytest.mark.parametrize("reverse", [False, True])
def test_ac_wins_and_previous_states_are_audit_only(previous, reverse):
    records = [
        row(previous, **{"MCR-Código rechazo": "R10", "MCR-Motivo rechazo": "FALTA DE FONDOS"}),
        row("AC", **{"MCR-Fecha pago": "02/09/2026", "MCR-Fecha acredit.": "15/09/2026", "Observación": ""}),
    ]
    raw = raw_frame(*(reversed(records) if reverse else records))
    before = raw.copy(deep=True)
    portfolio = build_portfolio(raw, CUTOFF)
    cheque = portfolio.iloc[0]

    assert len(portfolio) == 1
    assert portfolio["Importe"].sum() == pytest.approx(1000.25)
    assert cheque["Código estado"] == "AC"
    assert cheque["Estado calculado"] == "Acreditado"
    assert cheque["Duplicados consolidados"] == 1
    assert cheque[SOURCE_ROWS] == "4, 5"
    assert previous in cheque["Historial de estados"]
    assert cheque["Recibo relacionado"] == "756699"
    assert cheque["Motivo rechazo"] == ""
    assert "RECHAZADO" not in cheque["Alertas"]
    assert "PENDIENTE" not in cheque["Alertas"]
    assert pending_collection(portfolio).empty
    assert rejected_bank_summary(portfolio).empty
    assert len(rejected_then_accredited(portfolio)) == (1 if previous == "RC" else 0)
    pd.testing.assert_frame_equal(raw, before)


def test_rejected_then_accredited_table_has_required_audit_fields():
    raw = raw_frame(
        row("RC", **{"MCR-Motivo rechazo": "FALTA DE FONDOS"}),
        row("AC", **{"MCR-Fecha acredit.": "03/09/2026"}),
    )
    recovered = rejected_then_accredited(build_portfolio(raw, CUTOFF))
    assert list(recovered.columns) == ["Cliente", "N° cheque", "Importe", "Estado anterior", "Estado final", "Fecha", "Motivo de rechazo", SOURCE_ROWS]
    assert recovered.iloc[0]["Estado anterior"] == "RC - Rechazado"
    assert recovered.iloc[0]["Estado final"] == "AC - Acreditado"
    assert recovered.iloc[0]["Fecha"] == pd.Timestamp("2026-09-03")
    assert recovered.iloc[0]["Motivo de rechazo"] == "FALTA DE FONDOS"
    assert recovered.iloc[0][SOURCE_ROWS] == "4, 5"


def test_duplicate_ac_is_counted_once_and_keeps_latest_ac_date():
    raw = raw_frame(
        row("AC", number=12345.0, **{"MCR-Fecha acredit.": "01/09/2026"}),
        row("AC", number="12345", **{"MCR-Fecha acredit.": "03/09/2026"}),
    )
    portfolio = build_portfolio(raw, CUTOFF)
    assert len(portfolio) == 1
    assert portfolio.iloc[0]["Fecha estado final"] == pd.Timestamp("2026-09-03")
    assert portfolio.iloc[0]["Cantidad registros origen"] == 2
    assert not portfolio.iloc[0]["Rechazado luego acreditado"]


@pytest.mark.parametrize("number", [None, "", "0", 0])
def test_missing_numbers_are_never_merged_by_amount(number):
    portfolio = build_portfolio(raw_frame(row("RC", number), row("AC", number)), CUTOFF)
    assert len(portfolio) == 2
    assert portfolio["Duplicados consolidados"].sum() == 0
    assert portfolio["Estado calculado"].eq("Rechazado").sum() == 1


def test_same_number_different_amount_or_missing_amount_stays_separate():
    portfolio = build_portfolio(raw_frame(
        row("RC", amount=1000.25), row("AC", amount=1000.26),
        row("PS", amount=None), row("AC", amount=None),
    ), CUTOFF)
    assert len(portfolio) == 4


def test_rc_without_metadata_is_rejected_and_latest_rc_replaces_ps():
    portfolio = build_portfolio(raw_frame(
        row("PS"), row("r.c.", **{"MCR-Fecha pago": "03/09/2026"}),
    ), CUTOFF)
    assert len(portfolio) == 1
    assert portfolio.iloc[0]["Estado calculado"] == "Rechazado"


def test_rc_in_alternate_state_is_detected_when_main_is_not_an_operational_code():
    raw = raw_frame(row("MANU", **{"Estado": "RC"}))
    assert build_portfolio(raw, CUTOFF).iloc[0]["Estado calculado"] == "Rechazado"
    raw["MCR-Medio de pago"] = "MANU"
    movements = build_movement_control(raw)
    assert movements.iloc[0]["Estado operativo"] == "Rechazado"
    assert manual_accredited_movements(movements).empty


def test_number_and_amount_60_are_not_treated_as_missing():
    raw = raw_frame(row("PS", number=60, amount=60), row("AC", number=60, amount=60))
    portfolio = build_portfolio(raw, CUTOFF)
    assert len(portfolio) == 1
    assert portfolio.iloc[0]["Importe"] == 60
    assert portfolio.iloc[0]["N° cheque / eCheq"] == "60"
    assert build_movement_control(raw, portfolio).iloc[0]["N° cheque / eCheq"] == "60"


@pytest.mark.parametrize("state", [None, "", "MANU", "RE", "PE"])
def test_manu_without_ac_rc_ps_is_accredited_externally(state):
    raw = raw_frame(row(state, **{"MCR-Medio de pago": "MANU"}))
    movements = build_movement_control(raw, build_portfolio(raw, CUTOFF))
    assert movements.iloc[0]["Estado operativo"] == "Acreditado"
    assert movements.iloc[0]["Origen"] == MANUAL_ORIGIN
    assert len(manual_accredited_movements(movements)) == 1
    assert pending_movements_detail(movements).empty


@pytest.mark.parametrize(("state", "expected"), [("AC", "Acreditado"), ("rc", "Rechazado"), ("P.S.", "Pendiente de acreditación")])
def test_manu_with_explicit_state_keeps_ac_rc_or_ps(state, expected):
    movements = build_movement_control(raw_frame(row(state, **{"MCR-Medio de pago": "MANU"})))
    assert movements.iloc[0]["Estado operativo"] == expected
    assert manual_accredited_movements(movements).empty


def mixed_raw():
    return raw_frame(
        row("RC", **{"MCR-Motivo rechazo": "FALTA DE FONDOS"}),
        row("AC", **{"MCR-Fecha acredit.": "03/09/2026"}),
        row("PS", number="22222"),
        row("RC", number="33333"),
        row("", number="", **{"MCR-Medio de pago": "MANU"}),
        row("PS", number="", **{"MCR-Medio de pago": "TRANSFERENCIA"}),
    )


def test_unified_control_and_pending_table_do_not_reintroduce_history():
    raw = mixed_raw()
    portfolio = build_portfolio(raw, CUTOFF)
    movements = build_movement_control(raw, portfolio)
    assert len(movements) == 5
    assert len(movements[movements["Tipo de registro"].eq("Cheque")]) == len(portfolio) == 3
    pending = pending_movements_detail(movements)
    assert len(pending) == 2
    assert list(pending.columns) == ["Cliente", "Método de pago", "Fecha", "Importe", "N° cheque / eCheq", "Recibo relacionado", "Estado", "Origen", SOURCE_ROWS]
    assert set(pending["Método de pago"]) == {"CPD", "TRANSFERENCIA"}
    assert "12345" not in set(pending["N° cheque / eCheq"])


def test_full_excel_contains_every_category_and_all_source_rows():
    raw = mixed_raw()
    portfolio = build_portfolio(raw, CUTOFF)
    movements = build_movement_control(raw, portfolio)
    exported = export_movements_excel(movements, raw, CUTOFF)
    sheets = pd.read_excel(BytesIO(exported), sheet_name=None)
    expected_counts = {
        "Cartera consolidada": 3,
        "Movimientos pendientes": 2,
        "Rechazados efectivos": 1,
        "Rechazados luego acreditados": 1,
        "MANU acreditados": 1,
        "Datos fuente filtrados": 6,
    }
    for sheet, count in expected_counts.items():
        assert len(sheets[sheet]) == count
    assert sheets["Cartera consolidada"]["Importe"].sum() == pytest.approx(3000.75)


@pytest.mark.parametrize("exporter", ["portfolio", "movements"])
def test_filtered_export_includes_all_history_but_not_unrelated_rows(exporter):
    raw = mixed_raw()
    portfolio = build_portfolio(raw, CUTOFF).iloc[[0]]
    if exporter == "portfolio":
        exported = export_excel(portfolio, raw, CUTOFF)
    else:
        full_control = build_movement_control(raw, build_portfolio(raw, CUTOFF))
        filtered = full_control[full_control["ID movimiento"].eq(portfolio.iloc[0]["ID cartera"])]
        exported = export_movements_excel(filtered, raw, CUTOFF)
    sheets = pd.read_excel(BytesIO(exported), sheet_name=None)
    assert set(sheets["Datos fuente filtrados"]["Fila fuente"]) == {4, 5}
    assert len(sheets["Cartera consolidada"]) == 1
    assert len(sheets["Rechazados luego acreditados"]) == 1
    assert sheets["Movimientos pendientes"].empty
    assert sheets["MANU acreditados"].empty
    assert set(select_source_rows(portfolio, raw)["Fila fuente"]) == {4, 5}


def test_receipt_comparison_counts_one_consolidated_cheque():
    raw = raw_frame(row("PS"), row("AC"), row("AC", **{"MCR-Medio de pago": "TRANSFERENCIA"}))
    control = build_movement_control(raw, build_portfolio(raw, CUTOFF))
    comparison = receipt_reconciliation(control).iloc[0]
    assert comparison["Cheques"] == 1
    assert comparison["Resultado"] == "Coincide recibo e importe"


def test_manual_only_and_empty_filtered_excel_keep_category_sheets():
    raw = raw_frame(row("", number="", **{"MCR-Medio de pago": "MANU"}))
    control = build_movement_control(raw)
    for selected in (control, control.iloc[0:0]):
        sheets = pd.read_excel(BytesIO(export_movements_excel(selected, raw, CUTOFF)), sheet_name=None)
        assert "Estado calculado" in sheets["Cartera consolidada"].columns
        assert sheets["Cartera consolidada"].empty
        assert sheets["Movimientos pendientes"].empty
        assert len(sheets["MANU acreditados"]) == len(selected)
        assert len(sheets["Datos fuente filtrados"]) == len(selected)
