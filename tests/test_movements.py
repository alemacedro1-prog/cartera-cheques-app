from datetime import date
from io import BytesIO

import pandas as pd

from utils.movements import (
    build_movement_control,
    export_movements_excel,
    movement_link_summary,
    movement_type_summary,
    payment_method_group,
    receipt_reconciliation,
)


def movement_row(**changes):
    base = {
        "MCR-Medio de pago": "EFECTIVO",
        "MCR-Importe instr.": 1000,
        "MCR-Estado instr.": "AC",
        "MCR-Fecha pago": "18/08/2026",
        "MCR-Nombre cliente": "Cliente Uno",
        "MCR-CUIT cliente": "30-12345678-9",
        "MCR-Número de recibo": "75001",
        "Nro Cpb Relación": "",
        "Nro Cpb Relacionado": "",
        "Tipo Cpb Relacionado": "",
        "Observación": "",
        "Fila fuente": 2,
    }
    base.update(changes)
    return base


def test_payment_method_groups_common_real_world_variants():
    assert payment_method_group("EF") == "Efectivo"
    assert payment_method_group("Efectivo") == "Efectivo"
    assert payment_method_group("TRF") == "Transferencia"
    assert payment_method_group("Transferencia bancaria") == "Transferencia"
    assert payment_method_group("DEP") == "Depósito"
    assert payment_method_group("Depósito en cuenta") == "Depósito"
    assert payment_method_group("TARJETA") == "TARJETA"


def test_movement_control_includes_cheques_and_excludes_blank_media():
    raw = pd.DataFrame([
        movement_row(**{"MCR-Medio de pago": "EFECTIVO", "Fila fuente": 2}),
        movement_row(**{
            "MCR-Medio de pago": "CPD",
            "MCR-Número de recibo": "99999",
            "MCR-Número de cheque": "12345",
            "MCR-Fecha vencim.": "20/08/2026",
            "Observación": "Recibo 756699",
            "Fila fuente": 3,
        }),
        movement_row(**{"MCR-Medio de pago": "", "Fila fuente": 4}),
    ])

    result = build_movement_control(raw)

    assert len(result) == 2
    assert set(result["Tipo de registro"]) == {"Cheque", "Movimiento bancario"}
    cheque = result[result["Tipo de registro"].eq("Cheque")].iloc[0]
    assert cheque["Grupo medio de pago"] == "Cheque"
    assert cheque["N° cheque / eCheq"] == "12345"
    assert cheque["Estado vínculo"] == "Con recibo"
    assert cheque["Recibo relacionado"] == "756699"
    assert cheque["MCR-Número de recibo"] == "99999"


def test_receipt_rules_are_specific_and_auditable_by_payment_method():
    raw = pd.DataFrame([
        movement_row(**{
            "MCR-Medio de pago": "EFECTIVO",
            "MCR-Número de recibo": "75001",
            "Fila fuente": 2,
        }),
        movement_row(**{
            "MCR-Medio de pago": "TRANSFERENCIA",
            "MCR-Número de recibo": "",
            "Nro Cpb Relación": "819-591309",
            "Fila fuente": 3,
        }),
        movement_row(**{
            "MCR-Medio de pago": "DEPOSITO",
            "MCR-Número de recibo": "",
            "Observación": "Recibo Nro. 756699",
            "Fila fuente": 4,
        }),
    ])

    result = build_movement_control(raw).set_index("Grupo medio de pago")

    assert result.at["Efectivo", "Recibo relacionado"] == "75001"
    assert result.at["Efectivo", "Fuente del vínculo"] == "MCR-Número de recibo"
    assert result.at["Transferencia", "Recibo relacionado"] == "819-591309"
    assert result.at["Transferencia", "Fuente del vínculo"] == "Nro Cpb Relación"
    assert result.at["Depósito", "Recibo relacionado"] == "756699"
    assert result.at["Depósito", "Fuente del vínculo"] == "Observación"
    assert result["Estado vínculo"].eq("Con recibo").all()


def test_incompatible_receipt_sources_are_flagged_for_review():
    result = build_movement_control(pd.DataFrame([movement_row(**{
        "MCR-Medio de pago": "TRANSFERENCIA",
        "Nro Cpb Relación": "819-591309",
        "MCR-Número de recibo": "75001",
    })])).iloc[0]

    assert result["Estado vínculo"] == "A revisar"
    assert result["Fuente del vínculo"] == "Fuentes incompatibles"
    assert "Nro Cpb Relación: 819-591309" in result["Fuentes detectadas"]
    assert "MCR-Número de recibo: 75001" in result["Fuentes detectadas"]


def test_equivalent_receipt_formats_do_not_create_false_conflicts():
    result = build_movement_control(pd.DataFrame([movement_row(**{
        "MCR-Medio de pago": "TRANSFERENCIA",
        "Nro Cpb Relación": "819-591309",
        "MCR-Número de recibo": "819591309",
    })])).iloc[0]

    assert result["Estado vínculo"] == "Con recibo"
    assert result["Recibo relacionado"] == "819-591309"


def test_typed_related_number_is_used_only_when_it_is_a_receipt():
    raw = pd.DataFrame([
        movement_row(**{
            "MCR-Medio de pago": "TARJETA",
            "MCR-Número de recibo": "",
            "Nro Cpb Relacionado": "555",
            "Tipo Cpb Relacionado": "RECIBO",
            "Fila fuente": 2,
        }),
        movement_row(**{
            "MCR-Medio de pago": "TARJETA",
            "MCR-Número de recibo": "",
            "Nro Cpb Relacionado": "777",
            "Tipo Cpb Relacionado": "FACTURA",
            "Fila fuente": 3,
        }),
    ])

    result = build_movement_control(raw)

    assert result.iloc[0]["Estado vínculo"] == "Con recibo"
    assert result.iloc[0]["Recibo relacionado"] == "555"
    assert result.iloc[1]["Estado vínculo"] == "Sin recibo"


def test_receipt_reconciliation_compares_cheques_and_bank_movements():
    raw = pd.DataFrame([
        movement_row(**{
            "MCR-Medio de pago": "CPD",
            "MCR-Importe instr.": 1000,
            "MCR-Fecha vencim.": "20/08/2026",
            "Observación": "Recibo 75001",
            "Fila fuente": 2,
        }),
        movement_row(**{
            "MCR-Medio de pago": "TRANSFERENCIA",
            "MCR-Importe instr.": 1000,
            "Nro Cpb Relación": "75001",
            "MCR-Número de recibo": "",
            "Fila fuente": 3,
        }),
        movement_row(**{
            "MCR-Medio de pago": "CPD",
            "MCR-Importe instr.": 500,
            "MCR-Fecha vencim.": "21/08/2026",
            "Observación": "Recibo 75002",
            "Fila fuente": 4,
        }),
        movement_row(**{
            "MCR-Medio de pago": "EFECTIVO",
            "MCR-Importe instr.": 700,
            "MCR-Número de recibo": "75003",
            "Fila fuente": 5,
        }),
    ])

    reconciliation = receipt_reconciliation(build_movement_control(raw)).set_index("Recibo")

    assert reconciliation.at["75001", "Resultado"] == "Coincide recibo e importe"
    assert reconciliation.at["75001", "Diferencia"] == 0
    assert reconciliation.at["75002", "Resultado"] == "Solo en cheques"
    assert reconciliation.at["75003", "Resultado"] == "Solo en movimientos bancarios"


def test_movement_summary_and_excel_preserve_all_link_states():
    raw = pd.DataFrame([
        movement_row(**{"Fila fuente": 2}),
        movement_row(**{"MCR-Número de recibo": "", "Fila fuente": 3}),
        movement_row(**{
            "MCR-Medio de pago": "TRANSFERENCIA",
            "MCR-Número de recibo": "75002",
            "Nro Cpb Relación": "819-591310",
            "Observación": "=HYPERLINK(\"bad\")",
            "Fila fuente": 4,
        }),
    ])
    movements = build_movement_control(raw)

    summary = movement_link_summary(movements).set_index("Estado vínculo")
    assert summary.at["Con recibo", "Cantidad"] == 1
    assert summary.at["Sin recibo", "Cantidad"] == 1
    assert summary.at["A revisar", "Cantidad"] == 1
    type_summary = movement_type_summary(movements).set_index("Tipo de registro")
    assert type_summary.at["Cheque", "Cantidad"] == 0
    assert type_summary.at["Movimiento bancario", "Cantidad"] == 3

    exported = export_movements_excel(movements, raw, date(2026, 8, 18))
    exported_summary = pd.read_excel(BytesIO(exported), sheet_name="Resumen")
    exported_movements = pd.read_excel(BytesIO(exported), sheet_name="Movimientos")
    exported_reconciliation = pd.read_excel(BytesIO(exported), sheet_name="Cruce por recibo")
    exported_source = pd.read_excel(BytesIO(exported), sheet_name="Datos fuente filtrados")

    assert set(exported_summary["Indicador"]).issuperset({
        "Cheque - cantidad",
        "Movimiento bancario - cantidad",
        "Con recibo - cantidad",
        "Sin recibo - cantidad",
        "A revisar - cantidad",
    })
    assert len(exported_movements) == 3
    assert len(exported_reconciliation) == 1
    assert exported_reconciliation.iloc[0]["Resultado"] == "Solo en movimientos bancarios"
    assert len(exported_source) == 3
    assert exported_source.loc[exported_source["Fila fuente"].eq(4), "Observación"].item().startswith("'")
