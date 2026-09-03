"""Pruebas de pantalla aisladas: no modifican Secrets ni la autenticación real."""
from io import BytesIO
from pathlib import Path

import pandas as pd
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest


@pytest.mark.parametrize(("module", "section", "expected_heading"), [
    ("Control de movimientos y recibos", "Cobros del mes", "Movimientos pendientes"),
    ("Cartera de cheques", "Rescatados y rechazados", "Cheques rechazados que luego se acreditaron"),
    ("Cartera de cheques", "Reportes", "Descargar reportes"),
])
def test_consolidated_workflows_render_without_errors(monkeypatch, module, section, expected_heading):
    rows = pd.DataFrame([
        {"MCR-Medio de pago": "CPD", "MCR-Estado instr.": "RC", "MCR-Número de cheque": "1001", "MCR-Importe instr.": 500,
         "MCR-Fecha pago": "01/09/2026", "MCR-Motivo rechazo": "FALTA DE FONDOS"},
        {"MCR-Medio de pago": "CPD", "MCR-Estado instr.": "AC", "MCR-Número de cheque": "1001", "MCR-Importe instr.": 500,
         "MCR-Fecha pago": "02/09/2026"},
        {"MCR-Medio de pago": "CPD", "MCR-Estado instr.": "PS", "MCR-Número de cheque": "1002", "MCR-Importe instr.": 200,
         "MCR-Fecha pago": "03/09/2026", "MCR-Fecha vencim.": "10/09/2026"},
        {"MCR-Medio de pago": "MANU", "MCR-Estado instr.": "", "MCR-Importe instr.": 100, "MCR-Fecha pago": "03/09/2026"},
    ])
    upload = BytesIO()
    rows.to_excel(upload, index=False)
    upload.name = "CONRENPF_sintetico.xlsx"
    monkeypatch.setattr(st, "file_uploader", lambda *args, **kwargs: upload)
    original_segmented = st.segmented_control

    def choose(label, *args, **kwargs):
        if label == "Módulo":
            return module
        if label == "Sección":
            return section
        return original_segmented(label, *args, **kwargs)

    monkeypatch.setattr(st, "segmented_control", choose)
    app = AppTest.from_file(str(Path(__file__).parents[1] / "streamlit_app.py"), default_timeout=45)
    app.secrets["app"] = {"require_auth": False}
    app.run()
    assert not app.exception, [item.message for item in app.exception]
    assert expected_heading in [item.value for item in app.subheader]
    assert [item.label for item in app.metric[:5]] == [
        "Total de cheques", "Pendientes de cobro", "Acreditados", "Sin recibo asociado", "Con recibo asociado",
    ]
    assert [item.value for item in app.metric[:5]] == ["2", "1", "1", "2", "0"]
    assert app.subheader[0].value == "Resumen del archivo"
    if module == "Control de movimientos y recibos":
        pending_table = next(table.value for table in app.dataframe if "Método de pago" in table.value.columns)
        assert len(pending_table) == 1
        assert pending_table.iloc[0]["N° cheque / eCheq"] == "1002"


@pytest.mark.parametrize("selected_banks", [[], ["Macro"]])
def test_rejected_bank_chart_and_filter_use_deposit_not_issuer(monkeypatch, selected_banks):
    rows = pd.DataFrame([
        {"MCR-Medio de pago": "CPD", "MCR-Estado instr.": "RC", "MCR-Número de cheque": str(index),
         "MCR-Importe instr.": amount, "MCR-Banco": issuer, "Nombre del banco": deposit,
         "MCR-Nombre cliente": "Cliente de prueba", "MCR-Fecha pago": "01/09/2026"}
        for index, (issuer, deposit, amount) in enumerate([
            ("ICBC", "Macro", 300), ("CREDICOOP", "Galicia", 400), ("SANTA FE", "Nación", 600),
            ("Macro", "", 500), ("Galicia", "ICBC", 700),
        ], start=100)
    ])
    upload = BytesIO()
    rows.to_excel(upload, index=False)
    upload.name = "CONRENPF_bancos_sintetico.xlsx"
    monkeypatch.setattr(st, "file_uploader", lambda *args, **kwargs: upload)
    original_segmented, original_pills, original_chart = st.segmented_control, st.pills, st.altair_chart
    chart_specs = []

    def choose(label, *args, **kwargs):
        if label == "Módulo":
            return "Cartera de cheques"
        if label == "Sección":
            return "Rescatados y rechazados"
        return original_segmented(label, *args, **kwargs)

    def filter_bank(label, *args, **kwargs):
        if label == "Banco de depósito":
            return selected_banks
        return original_pills(label, *args, **kwargs)

    def capture_chart(chart, *args, **kwargs):
        if kwargs.get("key") == "chart_rejected_banks":
            chart_specs.append(chart.to_dict())
        return original_chart(chart, *args, **kwargs)

    monkeypatch.setattr(st, "segmented_control", choose)
    monkeypatch.setattr(st, "pills", filter_bank)
    monkeypatch.setattr(st, "altair_chart", capture_chart)
    app = AppTest.from_file(str(Path(__file__).parents[1] / "streamlit_app.py"), default_timeout=45)
    app.secrets["app"] = {"require_auth": False}
    app.run()
    assert not app.exception, [item.message for item in app.exception]
    assert "Cheques rechazados por banco de depósito" in [item.value for item in app.subheader]
    metrics = {item.label: item.value for item in app.metric}
    assert metrics["Macro · 1 rechazados"] == "$ 300"
    # El filtro Macro afecta el gráfico, pero no el resumen completo del archivo.
    assert [item.value for item in app.metric[:5]] == ["5", "0", "0", "5", "0"]
    summary = next(table.value for table in app.dataframe if "Importe rechazado" in table.value.columns and "Banco" in table.value.columns)
    assert summary["Importe rechazado"].sum() == (300 if selected_banks else 2500)
    assert summary["Cantidad de rechazados"].sum() == (1 if selected_banks else 5)
    if not selected_banks:
        exceptions = next(table.value for table in app.dataframe if "Banco de depósito agrupado" in table.value.columns)
        assert set(exceptions["Banco de depósito agrupado"]) == {"Sin banco de depósito", "Otros bancos de depósito"}
        assert exceptions["Importe"].sum() == 1200
    spec = chart_specs[-1]
    chart_rows = next(iter(spec["datasets"].values()))
    assert [item["Banco"] for item in chart_rows] == ["Macro", "Galicia", "Nación"]
    assert spec["layer"][0]["encoding"]["x"]["axis"]["tickCount"] == 5


@pytest.mark.parametrize("manual_only", [False, True])
def test_upload_overview_receipts_and_amounts_cover_all_cheque_states(monkeypatch, manual_only):
    rows = [{"MCR-Medio de pago": "MANU", "MCR-Estado instr.": "", "MCR-Importe instr.": 9999}]
    if not manual_only:
        rows.extend([
            {"MCR-Medio de pago": "CPD", "MCR-Estado instr.": state, "MCR-Número de cheque": number,
             "MCR-Importe instr.": amount, "Observación": receipt, "MCR-Fecha vencim.": "10/09/2026"}
            for state, number, amount, receipt in [
                ("RC", "1", 100, "756699"), ("AC", "1", 100, ""),
                ("PS", "2", 200, "819-591309"), ("RC", "3", 300, ""), ("RE", "4", 400, ""),
            ]
        ])
    upload = BytesIO()
    pd.DataFrame(rows).to_excel(upload, index=False)
    upload.name = "CONRENPF_resumen_sintetico.xlsx"
    monkeypatch.setattr(st, "file_uploader", lambda *args, **kwargs: upload)
    app = AppTest.from_file(str(Path(__file__).parents[1] / "streamlit_app.py"), default_timeout=45)
    app.secrets["app"] = {"require_auth": False}
    app.run()
    assert not app.exception, [item.message for item in app.exception]
    assert [item.value for item in app.metric[:5]] == (["0"] * 5 if manual_only else ["4", "1", "1", "2", "2"])
    amounts = [item.value for item in app.caption if item.value.startswith("Importe: **")][:5]
    assert amounts == (["Importe: **$ 0**"] * 5 if manual_only else [
        "Importe: **$ 1.000**", "Importe: **$ 200**", "Importe: **$ 100**", "Importe: **$ 700**", "Importe: **$ 300**",
    ])


def test_upload_overview_is_not_shown_before_file_is_loaded(monkeypatch):
    monkeypatch.setattr(st, "file_uploader", lambda *args, **kwargs: None)
    app = AppTest.from_file(str(Path(__file__).parents[1] / "streamlit_app.py"), default_timeout=45)
    app.secrets["app"] = {"require_auth": False}
    app.run()
    assert not app.exception
    assert "Resumen del archivo" not in [item.value for item in app.subheader]
    assert not app.metric
