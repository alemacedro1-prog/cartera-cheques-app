"""Consolidación de instrumentos y trazabilidad, sin alterar el CONRENPF."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import pandas as pd


SOURCE_ROWS = "Filas originales del CONRENPF"


def cheque_number(value) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip().upper()
    if text in {"", "0", "0.0", "NAN", "NONE", "<NA>"}:
        return ""
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    return text


def source_row_ids(frame: pd.DataFrame) -> set[int]:
    """Incluye representante e historial, también después de filtrar la vista."""
    rows: set[int] = set()
    for value in frame.get(SOURCE_ROWS, pd.Series(dtype=object)).dropna():
        rows.update(int(item) for item in re.findall(r"\d+", str(value)))
    rows.update(pd.to_numeric(frame.get("Fila fuente", pd.Series(dtype=int)), errors="coerce").dropna().astype(int))
    return rows


def select_source_rows(frame: pd.DataFrame, raw: pd.DataFrame) -> pd.DataFrame:
    source = raw.copy()
    if "Fila fuente" not in source:
        source["Fila fuente"] = source.index + 2
    return source[source["Fila fuente"].isin(source_row_ids(frame))].copy()


def _amount_key(value, valid=True):
    if not valid or pd.isna(value):
        return None
    try:
        amount = Decimal(str(value))
        return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if amount.is_finite() else None
    except InvalidOperation:
        return None


def _date(row: pd.Series):
    # La fecha de acreditación es un evento final; el vencimiento no lo es.
    columns = ("Fecha acreditación", "Fecha ingreso / pago") if row["Código estado"] == "AC" else ("Fecha ingreso / pago",)
    for name in columns:
        value = row.get(name)
        if value is not None and not pd.isna(value):
            return pd.Timestamp(value)
    return pd.NaT


def consolidate_cheques(rows: pd.DataFrame) -> pd.DataFrame:
    if rows.empty:
        return rows.copy()
    work = rows.copy().reset_index(drop=True)
    groups: dict[tuple, list[int]] = {}
    for index, row in work.iterrows():
        number = cheque_number(row.get("N° cheque / eCheq"))
        amount = _amount_key(row.get("Importe"), row.get("Importe informado", True))
        # Sin número o importe válido, cada fila es un instrumento independiente.
        key = (number, amount) if number and amount is not None else ("fila", index)
        groups.setdefault(key, []).append(index)

    records = []
    for indices in groups.values():
        group = work.loc[indices].copy()
        group["_fecha_evento"] = group.apply(_date, axis=1)
        ordered = group.sort_values(["_fecha_evento", "Fila fuente"], na_position="first", kind="stable")
        ac = ordered[ordered["Código estado"].eq("AC")]
        # AC siempre gana, independientemente del orden de filas o del corte.
        chosen = (ac if not ac.empty else ordered).iloc[-1].drop(labels="_fecha_evento").copy()
        history = ordered[ordered.index != chosen.name]
        rejected = ordered[ordered["Estado calculado"].eq("Rechazado")]
        source_ids = sorted(source_row_ids(group))
        chosen[SOURCE_ROWS] = ", ".join(map(str, source_ids))
        chosen["Cantidad registros origen"] = len(group)
        chosen["Duplicados consolidados"] = len(group) - 1
        chosen["Criterio consolidación"] = "N° cheque + importe" if cheque_number(chosen["N° cheque / eCheq"]) and _amount_key(chosen["Importe"], chosen.get("Importe informado", True)) is not None else "Fila individual sin clave completa"
        chosen["Historial de estados"] = " | ".join(
            f"Fila {int(item['Fila fuente'])}: {item['Código estado'] or item['Estado calculado']}"
            + (f" ({item['_fecha_evento']:%d/%m/%Y})" if pd.notna(item["_fecha_evento"]) else " (sin fecha)")
            for _, item in ordered.iterrows()
        )
        chosen["Estado anterior"] = " · ".join(dict.fromkeys(
            f"{item['Código estado']} - {item['Estado calculado']}" if item["Código estado"] else item["Estado calculado"]
            for _, item in history.iterrows()
        ))
        chosen["Fecha estado final"] = _date(chosen)
        chosen["Rechazado luego acreditado"] = not ac.empty and not rejected.empty
        chosen["Motivo rechazo anterior"] = " · ".join(dict.fromkeys(
            str(value).strip() for value in rejected["Motivo rechazo"].dropna() if str(value).strip()
        ))
        if chosen["Estado calculado"] == "Rechazado" and not chosen["Motivo rechazo"]:
            chosen["Motivo rechazo"] = chosen["Motivo rechazo anterior"]
        if not ac.empty:
            chosen["Estado calculado"] = "Acreditado"
            chosen["Código estado"] = "AC"
            chosen["Fuente clasificación"] = "Estado AC prioritario en cheque consolidado"
            chosen["Código rechazo"] = ""
            chosen["Motivo rechazo"] = ""
            chosen["Alertas"] = ""
        # Un recibo anterior sigue siendo auditable si falta en la fila final.
        if not chosen["Recibo relacionado"]:
            linked = ordered[ordered["Recibo relacionado"].ne("")]
            if not linked.empty:
                receipt_row = linked.iloc[-1]
                chosen["Recibo relacionado"] = receipt_row["Recibo relacionado"]
                chosen["Fuente del vínculo"] = f"{receipt_row['Fuente del vínculo']} · fila {int(receipt_row['Fila fuente'])}"
        chosen["Estado recibo"] = "Tomado" if chosen["Recibo relacionado"] else "Sin recibo asociado"
        alerts = [value for value in str(chosen["Alertas"]).split(" · ") if value and value != "SIN RECIBO ASOCIADO"]
        if not chosen["Recibo relacionado"]:
            alerts.append("SIN RECIBO ASOCIADO")
        chosen["Alertas"] = " · ".join(alerts)
        chosen["Estado final"] = f"{chosen['Código estado']} - {chosen['Estado calculado']}" if chosen["Código estado"] else chosen["Estado calculado"]
        chosen["ID cartera"] = f"CHQ-{len(records) + 1:04d}"
        records.append(chosen.to_dict())
    return pd.DataFrame.from_records(records)


def rejected_then_accredited(portfolio: pd.DataFrame) -> pd.DataFrame:
    columns = ["Cliente", "N° cheque", "Importe", "Estado anterior", "Estado final", "Fecha", "Motivo de rechazo", SOURCE_ROWS]
    if portfolio.empty or "Rechazado luego acreditado" not in portfolio:
        return pd.DataFrame(columns=columns)
    recovered = portfolio[portfolio["Rechazado luego acreditado"].fillna(False)].copy()
    return recovered.rename(columns={
        "N° cheque / eCheq": "N° cheque",
        "Fecha estado final": "Fecha",
        "Motivo rechazo anterior": "Motivo de rechazo",
    }).reindex(columns=columns).reset_index(drop=True)
