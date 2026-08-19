from __future__ import annotations

import pandas as pd

from utils.portfolio import BANK_FILTER_OPTIONS, bank_filter_group


def apply_operational_scope(portfolio: pd.DataFrame, scope: str) -> pd.DataFrame:
    """Aplica las vistas operativas con una única definición de "En cartera"."""
    if portfolio.empty:
        return portfolio.copy()
    if scope == "En cartera":
        return portfolio[portfolio["Estado recibo"].eq("Tomado")].copy()
    if scope == "Pend. acreditación":
        return portfolio[portfolio["Estado calculado"].eq("Pendiente de acreditación")].copy()
    if scope == "Rechazados":
        return portfolio[portfolio["Estado calculado"].eq("Rechazado")].copy()
    if scope == "Acreditados":
        return portfolio[portfolio["Estado calculado"].eq("Acreditado")].copy()
    return portfolio.copy()


def receipt_summary(portfolio: pd.DataFrame) -> pd.DataFrame:
    """Resume cantidad e importe de movimientos con y sin recibo asociado."""
    columns = ["Vínculo", "Cantidad", "Importe", "Importe promedio", "Participación cantidad", "Participación importe"]
    if portfolio.empty:
        return pd.DataFrame(columns=columns)

    working = portfolio.copy()
    taken = working.get("Estado recibo", pd.Series(index=working.index, dtype=object)).eq("Tomado")
    working["Vínculo"] = taken.map({True: "Con recibo", False: "Sin recibo"})
    amounts = working.get("Importe", pd.Series(0.0, index=working.index))
    working["Importe"] = pd.to_numeric(amounts, errors="coerce").fillna(0)
    summary = working.groupby("Vínculo", as_index=False).agg(
        Cantidad=("Importe", "size"),
        Importe=("Importe", "sum"),
        **{"Importe promedio": ("Importe", "mean")},
    )
    summary = summary.set_index("Vínculo").reindex(["Con recibo", "Sin recibo"], fill_value=0).reset_index()
    total_count = int(summary["Cantidad"].sum())
    total_amount = float(summary["Importe"].sum())
    summary["Participación cantidad"] = summary["Cantidad"] / total_count if total_count else 0.0
    summary["Participación importe"] = summary["Importe"] / total_amount if total_amount else 0.0
    return summary[columns]


def rejected_bank_summary(portfolio: pd.DataFrame) -> pd.DataFrame:
    """Resume rechazos únicamente para Macro, Galicia y Nación."""
    columns = ["Banco", "Cantidad de rechazados", "Importe rechazado", "Importe promedio", "Clientes afectados"]
    if portfolio.empty or "Estado calculado" not in portfolio:
        return pd.DataFrame(columns=columns)

    rejected = portfolio[portfolio["Estado calculado"].eq("Rechazado")].copy()
    if rejected.empty:
        return pd.DataFrame(columns=columns)
    rejected["Banco"] = rejected.get("Banco cheque", pd.Series(index=rejected.index, dtype=object)).map(bank_filter_group)
    rejected = rejected[rejected["Banco"].isin(BANK_FILTER_OPTIONS)].copy()
    if rejected.empty:
        return pd.DataFrame(columns=columns)

    amounts = rejected.get("Importe", pd.Series(0.0, index=rejected.index))
    rejected["Importe"] = pd.to_numeric(amounts, errors="coerce").fillna(0)
    rejected["Cliente"] = rejected.get("Cliente", pd.Series(index=rejected.index, dtype=object)).fillna("").astype(str).str.strip()
    summary = rejected.groupby("Banco", as_index=False).agg(
        **{
            "Cantidad de rechazados": ("Importe", "size"),
            "Importe rechazado": ("Importe", "sum"),
            "Importe promedio": ("Importe", "mean"),
            "Clientes afectados": ("Cliente", lambda values: values[values.ne("")].nunique()),
        }
    )
    order = {bank: index for index, bank in enumerate(BANK_FILTER_OPTIONS)}
    summary["_orden"] = summary["Banco"].map(order)
    return summary.sort_values("_orden").drop(columns="_orden")[columns].reset_index(drop=True)
