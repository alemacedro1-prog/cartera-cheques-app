from __future__ import annotations

from datetime import date

import pandas as pd

from utils.portfolio import BANK_FILTER_OPTIONS, PENDING_COLLECTION_STATES, bank_filter_group

PROCESSING_VERSION = "deposit-banks-v11"
OTHER_DEPOSIT_BANK = "Otros bancos de depósito"
MISSING_DEPOSIT_BANK = "Sin banco de depósito"


def deposit_bank_group(value) -> str:
    """No reemplaza un banco de depósito faltante por el banco emisor."""
    if value is None or pd.isna(value) or str(value).strip().upper() in {"", "0", "0.0", "NAN", "NONE", "<NA>"}:
        return MISSING_DEPOSIT_BANK
    return bank_filter_group(value) or OTHER_DEPOSIT_BANK


def rejected_bank_detail(portfolio: pd.DataFrame) -> pd.DataFrame:
    if portfolio.empty or "Estado calculado" not in portfolio:
        return portfolio.iloc[0:0].assign(**{"Banco de depósito agrupado": pd.Series(dtype=str)})
    rejected = portfolio[portfolio["Estado calculado"].eq("Rechazado")].copy()
    rejected["Banco de depósito agrupado"] = rejected.get(
        "Banco depósito", pd.Series(index=rejected.index, dtype=object)
    ).map(deposit_bank_group)
    return rejected


def pending_collection(portfolio: pd.DataFrame) -> pd.DataFrame:
    """Devuelve los cheques que todavía representan un cobro pendiente."""
    if portfolio.empty:
        return portfolio.copy()
    return portfolio[portfolio["Estado calculado"].isin(PENDING_COLLECTION_STATES)].copy()


def pending_for_month(portfolio: pd.DataFrame, cutoff: date | pd.Timestamp) -> pd.DataFrame:
    """Devuelve cobros pendientes cuya fecha prevista pertenece al mes analizado."""
    pending = pending_collection(portfolio)
    if pending.empty:
        return pending
    expected = pd.to_datetime(
        pending.get("Fecha prevista de cobro", pd.Series(pd.NaT, index=pending.index)), dayfirst=True, errors="coerce"
    )
    month_start = pd.Timestamp(cutoff).to_period("M").to_timestamp()
    month_end = month_start + pd.offsets.MonthEnd(0)
    return pending[expected.between(month_start, month_end, inclusive="both")].copy()


def apply_operational_scope(
    portfolio: pd.DataFrame,
    scope: str,
    cutoff: date | pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Aplica vistas operativas con nombres pensados para usuarios no técnicos."""
    if portfolio.empty:
        return portfolio.copy()
    if scope == "Pendientes del mes":
        return pending_for_month(portfolio, cutoff or pd.Timestamp.today())
    if scope == "Todos los pendientes":
        return pending_collection(portfolio)
    if scope == "En cartera":
        return portfolio[portfolio["Estado recibo"].eq("Tomado")].copy()
    if scope in {"Pend. acreditación", "Pendientes de acreditación"}:
        return portfolio[portfolio["Estado calculado"].eq("Pendiente de acreditación")].copy()
    if scope in {"Rechazados", "Rechazados (RC)"}:
        return portfolio[portfolio["Estado calculado"].eq("Rechazado")].copy()
    if scope in {"Rescatados", "Rescatados (RE)"}:
        return portfolio[portfolio["Estado calculado"].eq("Rescatado")].copy()
    if scope in {"Acreditados", "Acreditados (AC)"}:
        return portfolio[portfolio["Estado calculado"].eq("Acreditado")].copy()
    return portfolio.copy()


def collection_calendar_summary(portfolio: pd.DataFrame, cutoff: date | pd.Timestamp) -> pd.DataFrame:
    """Agrupa por día los cobros pendientes del mes seleccionado."""
    columns = ["Fecha prevista de cobro", "Situación", "Cantidad", "Importe", "Clientes"]
    monthly = pending_for_month(portfolio, cutoff)
    if monthly.empty:
        return pd.DataFrame(columns=columns)
    monthly["Fecha prevista de cobro"] = pd.to_datetime(
        monthly["Fecha prevista de cobro"], dayfirst=True, errors="coerce"
    )
    cutoff_ts = pd.Timestamp(cutoff).normalize()
    monthly["Situación"] = monthly["Fecha prevista de cobro"].map(
        lambda value: "Fecha ya cumplida" if value < cutoff_ts else "Próximo cobro"
    )
    monthly["Importe"] = pd.to_numeric(monthly["Importe"], errors="coerce").fillna(0)
    monthly["Cliente"] = monthly.get("Cliente", pd.Series(index=monthly.index, dtype=object)).fillna("")
    summary = monthly.groupby(["Fecha prevista de cobro", "Situación"], as_index=False).agg(
        Cantidad=("Importe", "size"),
        Importe=("Importe", "sum"),
        Clientes=("Cliente", lambda values: values[values.ne("")].nunique()),
    )
    return summary[columns].sort_values("Fecha prevista de cobro").reset_index(drop=True)


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
    """Resume RC efectivos por banco de depósito, nunca por banco girado."""
    columns = ["Banco", "Cantidad de rechazados", "Importe rechazado", "Importe promedio", "Clientes afectados"]
    if portfolio.empty or "Estado calculado" not in portfolio:
        return pd.DataFrame(columns=columns)

    rejected = rejected_bank_detail(portfolio)
    if rejected.empty:
        return pd.DataFrame(columns=columns)
    rejected["Banco"] = rejected["Banco de depósito agrupado"]

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
    display_order = [*BANK_FILTER_OPTIONS, *(
        bank for bank in (OTHER_DEPOSIT_BANK, MISSING_DEPOSIT_BANK) if bank in set(rejected["Banco"])
    )]
    summary = summary.set_index("Banco").reindex(display_order, fill_value=0).reset_index()
    order = {bank: index for index, bank in enumerate(display_order)}
    summary["_orden"] = summary["Banco"].map(order)
    return summary.sort_values("_orden").drop(columns="_orden")[columns].reset_index(drop=True)
