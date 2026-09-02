from datetime import date
from io import BytesIO

import pandas as pd

from utils.movements import (
    build_movement_control,
    export_movements_excel,
    movement_link_summary,
    payment_method_group,
)


def movement_row(**changes):
    base = {
        "MCR-Medio de pago": "EFECTIVO",
        "MCR-Importe instr.": 1000,
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


def test_movement_control_excludes_cheques_and_blank_media():
    raw = pd.DataFrame([
        movement_row(**{"MCR-Medio de pago": "EFECTIVO", "Fila fuente": 2}),
        movement_row(**{"MCR-Medio de pago": "CPD", "Fila fuente": 3}),
        movement_row(**{"MCR-Medio de pago": "", "Fila fuente": 4}),
    ])

    result = build_movement_control(raw)

    assert len(result) == 1
    assert result.iloc[0]["Grupo medio de pago"] == "Efectivo"


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

    exported = export_movements_excel(movements, raw, date(2026, 8, 18))
    exported_summary = pd.read_excel(BytesIO(exported), sheet_name="Resumen")
    exported_movements = pd.read_excel(BytesIO(exported), sheet_name="Movimientos")
    exported_source = pd.read_excel(BytesIO(exported), sheet_name="Datos fuente filtrados")

    assert set(exported_summary["Indicador"]).issuperset({
        "Con recibo - cantidad",
        "Sin recibo - cantidad",
        "A revisar - cantidad",
    })
    assert len(exported_movements) == 3
    assert len(exported_source) == 3
    assert exported_source.loc[exported_source["Fila fuente"].eq(4), "Observación"].item().startswith("'")
