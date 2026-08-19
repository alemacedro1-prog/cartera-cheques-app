from datetime import date
from io import BytesIO

import pandas as pd
import pytest

from utils.portfolio import (
    ALLOWED_TYPES,
    BANK_FILTER_OPTIONS,
    ConcentradorError,
    bank_filter_group,
    build_portfolio,
    export_excel,
    read_concentrador,
    rejected_monthly_summary,
    validate_upload,
)
from utils.reports import export_portfolio_pdf


def row(**changes):
    base = {"MCR-Medio de pago": "CPD", "MCR-Importe instr.": 1000, "MCR-Estado instr.": "PE", "Fila fuente": 2,
            "MCR-Fecha vencim.": "20/08/2026", "MCR-Número de recibo": "900", "Nro Cpb Relación": "900"}
    base.update(changes)
    return base


def test_bank_filter_exposes_only_the_three_operational_banks():
    assert BANK_FILTER_OPTIONS == ("Macro", "Galicia", "Nación")


@pytest.mark.parametrize(("raw_bank", "expected"), [
    ("285", "Macro"),
    ("0285", "Macro"),
    ("Banco Macro S.A.", "Macro"),
    ("7", "Galicia"),
    ("007", "Galicia"),
    ("Banco de Galicia y Buenos Aires", "Galicia"),
    ("11", "Nación"),
    ("011", "Nación"),
    ("Banco de la Nación Argentina", "Nación"),
    ("NACIÓN", "Nación"),
    ("72", ""),
])
def test_bank_filter_groups_codes_and_names(raw_bank, expected):
    assert bank_filter_group(raw_bank) == expected


def make_xlsx(rows, preamble=2):
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        pd.DataFrame(rows).to_excel(writer, index=False, startrow=preamble)
    return output.getvalue()


def test_physical_receipt_uses_relation_and_ignores_mcr_receipt():
    frame = pd.DataFrame([row(**{"Nro Cpb Relación": "111", "Nro Cpb Relacionado": "222", "MCR-Número de recibo": "333"})])
    result = build_portfolio(frame, date(2026, 8, 18)).iloc[0]
    assert result["Recibo relacionado"] == "111"
    assert result["Fuente del vínculo"] == "Nro Cpb Relación"
    assert result["Estado recibo"] == "Tomado"
    assert "MCR-Número de recibo" not in result.index
    assert "Validación recibo" not in result.index


@pytest.mark.parametrize("kind,text,expected", [
    ("ECHEQ", "Existe en resumen bancario: 12345", "12345"),
    ("ECHEQDIF", "Existe en la recaudadora Nro. 98-76", "98-76"),
])
def test_echeq_receipt_from_observation(kind, text, expected):
    frame = pd.DataFrame([row(**{"MCR-Medio de pago": kind, "Observación": text, "MCR-Número de recibo": expected})])
    result = build_portfolio(frame, date(2026, 8, 18)).iloc[0]
    assert result["Recibo relacionado"] == expected
    assert result["Fuente del vínculo"] == "Observación"


def test_observation_is_the_physical_receipt_fallback():
    frame = pd.DataFrame([row(**{"Nro Cpb Relación": "", "Observación": "Recibo Nro. 45678", "MCR-Número de recibo": "999"})])
    result = build_portfolio(frame, date(2026, 8, 18)).iloc[0]
    assert result["Recibo relacionado"] == "45678"
    assert result["Fuente del vínculo"] == "Observación"


@pytest.mark.parametrize("receipt", ["750", "756699", "756701", "75000001", "751234567890123456"])
def test_internal_collection_receipt_starting_with_75_is_taken_from_observation(receipt):
    frame = pd.DataFrame([row(**{
        "Nro Cpb Relación": "",
        "Observación": f"Existe en resumen bancario.  {receipt}",
    })])

    result = build_portfolio(frame, date(2026, 8, 18)).iloc[0]

    assert result["Recibo relacionado"] == receipt
    assert result["Fuente del vínculo"] == "Observación"
    assert result["Estado recibo"] == "Tomado"
    assert "SIN RECIBO ASOCIADO" not in result["Alertas"]


def test_internal_collection_receipt_does_not_match_when_75_is_inside_a_longer_number():
    frame = pd.DataFrame([row(**{
        "Nro Cpb Relación": "",
        "Observación": "Referencia externa 1756699 sin recibo",
    })])

    result = build_portfolio(frame, date(2026, 8, 18)).iloc[0]

    assert result["Estado recibo"] == "Sin recibo asociado"


@pytest.mark.parametrize("kind", ALLOWED_TYPES)
def test_bare_hyphenated_receipt_in_observation_is_taken_for_every_movement(kind):
    frame = pd.DataFrame([row(**{
        "MCR-Medio de pago": kind,
        "Observación": "Movimiento ya tomado 819-591309",
        "Nro Cpb Relación": "111",
    })])
    result = build_portfolio(frame, date(2026, 8, 18)).iloc[0]
    assert result["Recibo relacionado"] == "819-591309"
    assert result["Fuente del vínculo"] == "Observación"
    assert result["Estado recibo"] == "Tomado"
    assert "SIN RECIBO ASOCIADO" not in result["Alertas"]


@pytest.mark.parametrize("kind", ALLOWED_TYPES)
def test_relation_receipt_is_the_fallback_for_every_movement(kind):
    frame = pd.DataFrame([row(**{
        "MCR-Medio de pago": kind,
        "Observación": "Movimiento sin número en la observación",
        "Nro Cpb Relación": "819-591310",
    })])
    result = build_portfolio(frame, date(2026, 8, 18)).iloc[0]
    assert result["Recibo relacionado"] == "819-591310"
    assert result["Fuente del vínculo"] == "Nro Cpb Relación"
    assert result["Estado recibo"] == "Tomado"


def test_mcr_and_related_receipt_fields_are_not_used_for_collection():
    frame = pd.DataFrame([row(**{"Nro Cpb Relación": "", "Nro Cpb Relacionado": "222", "MCR-Número de recibo": "333"})])
    result = build_portfolio(frame, date(2026, 8, 18)).iloc[0]
    assert result["Recibo relacionado"] == ""
    assert result["Fuente del vínculo"] == "Sin vínculo detectado"
    assert result["Estado recibo"] == "Sin recibo asociado"
    assert "SIN RECIBO ASOCIADO" in result["Alertas"]


def test_rejection_metadata_does_not_override_accredited_state():
    frame = pd.DataFrame([row(**{"MCR-Estado instr.": "AC", "MCR-Código rechazo": "R1", "MCR-Fecha acredit.": "17/08/2026"})])
    assert build_portfolio(frame, date(2026, 8, 18)).iloc[0]["Estado calculado"] == "Acreditado"


def test_ps_is_pending_accreditation_even_with_rejection_metadata():
    frame = pd.DataFrame([row(**{
        "MCR-Estado instr.": "PS",
        "MCR-Código rechazo": "R1",
        "MCR-Motivo rechazo": "Dato histórico",
        "MCR-Fecha acredit.": "17/08/2026",
    })])

    result = build_portfolio(frame, date(2026, 8, 18)).iloc[0]

    assert result["Estado calculado"] == "Pendiente de acreditación"
    assert "PENDIENTE DE ACREDITACIÓN" in result["Alertas"]
    assert "RECHAZADO" not in result["Alertas"]


@pytest.mark.parametrize("raw_state", ["PS", "ps ", "P.S.", "P/S", "PS - pendiente", "Pendiente de acreditación (PS)"])
def test_ps_variants_are_normalized(raw_state):
    result = build_portfolio(pd.DataFrame([row(**{"MCR-Estado instr.": raw_state})]), date(2026, 8, 18)).iloc[0]
    assert result["Código estado"] == "PS"
    assert result["Estado calculado"] == "Pendiente de acreditación"


@pytest.mark.parametrize("system_state", ["RE", "RC"])
def test_only_rejected_system_states_are_rejected(system_state):
    result = build_portfolio(pd.DataFrame([row(**{"MCR-Estado instr.": system_state})]), date(2026, 8, 18)).iloc[0]
    assert result["Estado calculado"] == "Rechazado"


def test_ps_amount_is_excluded_from_rejected_total():
    frame = pd.DataFrame([
        row(**{"MCR-Estado instr.": "RE", "MCR-Importe instr.": 24_803_810.71, "Fila fuente": 2}),
        row(**{
            "MCR-Estado instr.": "PS",
            "MCR-Importe instr.": 124_196_189.29,
            "MCR-Código rechazo": "R1",
            "MCR-Motivo rechazo": "Dato histórico",
            "Fila fuente": 3,
        }),
    ])

    portfolio = build_portfolio(frame, date(2026, 8, 18))
    rejected_total = portfolio.loc[portfolio["Estado calculado"].eq("Rechazado"), "Importe"].sum()

    assert rejected_total == pytest.approx(24_803_810.71)
    assert portfolio.loc[portfolio["Código estado"].eq("PS"), "Estado calculado"].item() == "Pendiente de acreditación"


def test_rejected_monthly_summary_groups_by_client_and_uses_date_fallback():
    portfolio = pd.DataFrame([
        {"Estado calculado": "Rechazado", "Cliente": "Cliente Norte", "Importe": 1200, "Fecha ingreso / pago": "05/07/2026", "Fecha vencimiento": "20/07/2026"},
        {"Estado calculado": "Rechazado", "Cliente": "Cliente Norte", "Importe": 800, "Fecha ingreso / pago": "21/07/2026", "Fecha vencimiento": "25/07/2026"},
        {"Estado calculado": "Rechazado", "Cliente": "Cliente Sur", "Importe": 3000, "Fecha ingreso / pago": None, "Fecha vencimiento": "02/08/2026"},
        {"Estado calculado": "Pendiente", "Cliente": "Cliente Norte", "Importe": 9999, "Fecha ingreso / pago": "10/07/2026", "Fecha vencimiento": "30/07/2026"},
    ])

    summary = rejected_monthly_summary(portfolio)

    north = summary[(summary["Cliente"] == "Cliente Norte") & (summary["Mes"] == pd.Timestamp("2026-07-01"))].iloc[0]
    south = summary[(summary["Cliente"] == "Cliente Sur") & (summary["Mes"] == pd.Timestamp("2026-08-01"))].iloc[0]
    assert north["Cantidad de rechazados"] == 2
    assert north["Importe rechazado"] == 2000
    assert north["Importe promedio"] == 1000
    assert south["Cantidad de rechazados"] == 1
    assert south["Importe rechazado"] == 3000
    assert summary["Cantidad de rechazados"].sum() == 3


def test_rejected_monthly_summary_is_empty_without_rejections():
    portfolio = pd.DataFrame([{"Estado calculado": "Pendiente", "Importe": 1000}])
    assert rejected_monthly_summary(portfolio).empty


@pytest.mark.parametrize("due,state,bucket", [("17/08/2026", "Vencido", "Vencidos"), ("18/08/2026", "Vence hoy", "Hoy"), ("25/08/2026", "Pendiente", "1-7 días"), (None, "Sin vencimiento", "Sin vencimiento")])
def test_due_boundaries(due, state, bucket):
    result = build_portfolio(pd.DataFrame([row(**{"MCR-Fecha vencim.": due})]), date(2026, 8, 18)).iloc[0]
    assert (result["Estado calculado"], result["Tramo vencimiento"]) == (state, bucket)


def test_reads_synthetic_concentrador_and_preserves_source_row():
    raw = read_concentrador(make_xlsx([row()]))
    assert len(raw) == 1 and raw.iloc[0]["Fila fuente"] == 4


def test_rejects_non_xlsx_and_oversized(monkeypatch):
    with pytest.raises(ConcentradorError): validate_upload(b"not an excel")
    monkeypatch.setattr("utils.portfolio.MAX_FILE_BYTES", 2)
    with pytest.raises(ConcentradorError): validate_upload(b"PKx")


def test_export_is_filtered_and_neutralizes_formulas():
    raw = pd.DataFrame([
        row(**{"Fila fuente": 2, "Observación": "=HYPERLINK(\"bad\")", "Nro Cpb Relacionado": "IGNORAR"}),
        row(**{"Fila fuente": 3}),
    ])
    portfolio = build_portfolio(raw, date(2026, 8, 18)).iloc[[0]]
    exported = export_excel(portfolio, raw, date(2026, 8, 18))
    cartera = pd.read_excel(BytesIO(exported), sheet_name="Cartera")
    source = pd.read_excel(BytesIO(exported), sheet_name="Datos fuente filtrados")
    assert "MCR-Número de recibo" not in cartera.columns
    assert "MCR-Número de recibo" not in source.columns
    assert "Nro Cpb Relacionado" not in source.columns
    assert len(source) == 1
    assert source.iloc[0]["Observación"].startswith("'")


def test_pdf_export_contains_summary_and_complete_detail():
    portfolio = build_portfolio(pd.DataFrame([
        row(**{"MCR-Nombre cliente": "Cliente Norte", "MCR-Número de cheque": "1001", "Fila fuente": 2}),
        row(**{"MCR-Nombre cliente": "Cliente Sur", "MCR-Número de cheque": "1002", "MCR-Estado instr.": "RE", "Fila fuente": 3}),
    ]), date(2026, 8, 18))

    exported = export_portfolio_pdf(portfolio, date(2026, 8, 18))

    assert exported.startswith(b"%PDF")
    assert len(exported) > 5_000
    assert exported.count(b"/Type /Page") >= 2
