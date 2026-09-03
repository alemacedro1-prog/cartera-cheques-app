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
    if module == "Control de movimientos y recibos":
        pending_table = next(table.value for table in app.dataframe if "Método de pago" in table.value.columns)
        assert len(pending_table) == 1
        assert pending_table.iloc[0]["N° cheque / eCheq"] == "1002"
