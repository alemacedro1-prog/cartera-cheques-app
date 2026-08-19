from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from datetime import date, timedelta

import altair as alt
import pandas as pd
import streamlit as st

from utils.analytics import apply_operational_scope, receipt_summary, rejected_bank_summary
from utils.portfolio import (
    ALLOWED_TYPES,
    ConcentradorError,
    export_excel,
    format_currency,
    portfolio_from_bytes,
    rejected_monthly_summary,
)
from utils.reports import export_portfolio_pdf
from utils.security import (
    email_is_allowed,
    normalize_username,
    password_is_valid,
    token_is_current,
    validate_allowed_emails,
    validate_password_users,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOGGER = logging.getLogger("cartera")
PROCESSING_RULE_VERSION = "2026-08-19-receipts-cutoff-bank-stats-v6"
BANK_FILTER_OPTIONS = ("Macro", "Galicia", "Nación")


def bank_filter_group(value) -> str:
    """Normaliza los tres bancos del filtro sin depender de un módulo en caché."""
    text = "" if value is None or pd.isna(value) else str(value).strip()
    if not text:
        return ""
    if text.endswith(".0") and re.fullmatch(r"\d+\.0", text):
        text = text[:-2]

    numeric = re.fullmatch(r"0*(\d+)", text)
    if numeric:
        by_code = {"7": "Galicia", "11": "Nación", "285": "Macro"}
        if numeric.group(1) in by_code:
            return by_code[numeric.group(1)]

    normalized = unicodedata.normalize("NFKD", text)
    normalized = "".join(character for character in normalized if not unicodedata.combining(character)).upper()
    if "MACRO" in normalized:
        return "Macro"
    if "GALICIA" in normalized:
        return "Galicia"
    if "NACION" in normalized:
        return "Nación"
    return ""

st.set_page_config(page_title="Cartera de cheques", page_icon=":material/account_balance_wallet:", layout="wide")


def secret_section(name: str) -> dict:
    try:
        value = st.secrets.get(name, {})
        return dict(value) if value else {}
    except (FileNotFoundError, KeyError):
        return {}


def require_password_access() -> None:
    password_settings = secret_section("password_auth")
    configured_users = password_settings.get("users", [])
    try:
        allowed_users = validate_password_users(configured_users)
    except ValueError as error:
        LOGGER.error("Configuración de acceso por contraseña inválida: %s", error)
        st.error(f"Configuración de acceso inválida. {error}", icon=":material/error:")
        st.stop()

    authenticated_username = normalize_username(st.session_state.get("authenticated_username"))
    allowed_names = {user["username"] for user in allowed_users}
    if authenticated_username in allowed_names:
        with st.sidebar:
            st.caption(f"Sesión: {authenticated_username.upper()}")
            if st.button("Cerrar sesión", icon=":material/logout:", key="password_logout"):
                st.session_state.pop("authenticated_username", None)
                st.rerun()
        return

    st.title("Cartera de cheques")
    st.caption("Ingresá con un usuario autorizado para acceder a la cartera.")
    with st.container(border=True):
        with st.form("password_login", clear_on_submit=False):
            username = st.text_input("Usuario", autocomplete="username")
            password = st.text_input("Contraseña", type="password", autocomplete="current-password")
            submitted = st.form_submit_button(
                "Ingresar",
                icon=":material/login:",
                type="primary",
                width="stretch",
            )
        if submitted:
            if password_is_valid(username, password, allowed_users):
                st.session_state["authenticated_username"] = normalize_username(username)
                st.rerun()
            st.error("Usuario o contraseña incorrectos.", icon=":material/lock:")
    st.stop()


def require_access() -> None:
    settings = secret_section("app")
    require_auth = bool(settings.get("require_auth", False))
    if not require_auth:
        st.warning("Modo local sin autenticación. No usar esta configuración en internet.", icon=":material/warning:")
        return
    auth_mode = str(settings.get("auth_mode", "oidc")).strip().casefold()
    if auth_mode == "password":
        require_password_access()
        return
    if auth_mode != "oidc":
        st.error("Configuración de acceso inválida. El modo debe ser 'oidc' o 'password'.", icon=":material/error:")
        st.stop()
    try:
        allowed_emails = validate_allowed_emails(settings.get("allowed_emails", []))
    except ValueError as error:
        LOGGER.error("Configuración de acceso inválida: %s", error)
        st.error(f"Configuración de acceso inválida. {error}", icon=":material/error:")
        st.stop()
    try:
        logged_in = bool(st.user.is_logged_in)
    except (AttributeError, KeyError):
        logged_in = False
    if not logged_in:
        st.title("Cartera de cheques")
        st.write("Ingresá con una cuenta autorizada para continuar.")
        if st.button("Ingresar", icon=":material/login:", type="primary"):
            st.login()
        st.stop()
    user = dict(st.user)
    if not token_is_current(user):
        st.error("La sesión venció. Volvé a ingresar.")
        if st.button("Renovar acceso", icon=":material/login:"):
            st.logout()
        st.stop()
    if not email_is_allowed(user.get("email") or user.get("preferred_username"), allowed_emails):
        LOGGER.warning("Acceso rechazado para identidad no autorizada")
        st.error("Tu cuenta no está autorizada para usar esta aplicación.")
        if st.button("Salir", icon=":material/logout:"):
            st.logout()
        st.stop()
    with st.sidebar:
        st.caption(f"Sesión: {user.get('email') or user.get('preferred_username', 'usuario autorizado')}")
        if st.button("Cerrar sesión", icon=":material/logout:"):
            st.logout()


@st.cache_data(ttl="15m", max_entries=4, show_spinner="Procesando el concentrador…", scope="session")
def process_file(file_bytes: bytes, cutoff: date, rule_version: str):
    LOGGER.debug("Regla de procesamiento: %s", rule_version)
    return portfolio_from_bytes(file_bytes, cutoff)


@st.cache_data(ttl="5m", max_entries=2, show_spinner="Preparando el Excel…", scope="session")
def make_excel(portfolio: pd.DataFrame, raw: pd.DataFrame, cutoff: date) -> bytes:
    return export_excel(portfolio, raw, cutoff)


@st.cache_data(ttl="10m", max_entries=2, show_spinner="Generando el PDF profesional…", scope="session")
def make_pdf(portfolio: pd.DataFrame, cutoff: date) -> bytes:
    return export_portfolio_pdf(portfolio, cutoff)


def donut_chart(data: pd.DataFrame, category: str, title: str, key: str, max_slices: int = 7) -> None:
    grouped = data[[category, "Importe"]].copy()
    grouped[category] = grouped[category].fillna("").astype(str).str.strip().replace("", "Sin dato")
    grouped["Importe"] = pd.to_numeric(grouped["Importe"], errors="coerce").fillna(0).clip(lower=0)
    grouped = grouped.groupby(category, as_index=False)["Importe"].sum()
    grouped = grouped[grouped["Importe"] > 0].sort_values("Importe", ascending=False)
    if grouped.empty:
        st.info("No hay datos para esta visualización.")
        return
    if len(grouped) > max_slices:
        visible = grouped.head(max_slices - 1).copy()
        other_label = "Otros clientes" if category == "Cliente" else "Otros"
        grouped = pd.concat([
            visible,
            pd.DataFrame([{category: other_label, "Importe": grouped.iloc[max_slices - 1:]["Importe"].sum()}]),
        ], ignore_index=True)
    total = float(grouped["Importe"].sum())
    grouped["Porcentaje"] = grouped["Importe"] / total
    dark_theme = st.context.theme.type == "dark"
    foreground = "#E7EEF8" if dark_theme else "#23364A"
    separator = "#0B1220" if dark_theme else "#FFFFFF"
    palette = ["#67B7F7", "#5DD39E", "#B99AF5", "#FF8B86", "#F8C65A", "#38C5D8", "#9AA9BC"] if dark_theme else ["#245A8D", "#2E8B70", "#7957B8", "#C95651", "#C48717", "#238EA3", "#65758B"]
    base = alt.Chart(grouped)
    arcs = base.mark_arc(innerRadius=68, outerRadius=122, padAngle=0.015, cornerRadius=4, stroke=separator, strokeWidth=2).encode(
        theta=alt.Theta("Importe:Q", stack=True),
        color=alt.Color(
            f"{category}:N",
            title=None,
            scale=alt.Scale(range=palette),
            legend=alt.Legend(orient="bottom", direction="horizontal", columns=2, labelLimit=190, symbolType="circle"),
        ),
        order=alt.Order("Importe:Q", sort="descending"),
        tooltip=[
            alt.Tooltip(f"{category}:N", title=category),
            alt.Tooltip("Importe:Q", title="Importe", format="$,.2f"),
            alt.Tooltip("Porcentaje:Q", title="Participación", format=".1%"),
        ],
    )
    labels = base.mark_text(radius=96, fontSize=11, fontWeight=700, color="#FFFFFF").encode(
        theta=alt.Theta("Importe:Q", stack=True),
        order=alt.Order("Importe:Q", sort="descending"),
        text=alt.condition(alt.datum.Porcentaje >= 0.075, alt.Text("Porcentaje:Q", format=".0%"), alt.value("")),
    )
    center = alt.Chart(pd.DataFrame({"Total": [format_currency(total)]})).mark_text(
        color=foreground, fontSize=16, fontWeight=700
    ).encode(text=alt.Text("Total:N"))
    chart = (arcs + labels + center).properties(height=325).configure_view(stroke=None).configure_legend(
        labelColor=foreground, titleColor=foreground, labelFontSize=12
    )
    st.subheader(title)
    st.altair_chart(chart, key=key)


def due_flow_chart(data: pd.DataFrame, cutoff: date) -> None:
    st.subheader("Flujo de vencimientos")
    st.caption("Importe a vencer por semana durante los próximos 90 días.")
    cutoff_ts = pd.Timestamp(cutoff)
    end_ts = cutoff_ts + pd.Timedelta(days=90)
    due = data[data["Estado calculado"].isin(["Pendiente", "Vence hoy"])].copy()
    due["Fecha vencimiento"] = pd.to_datetime(due["Fecha vencimiento"], errors="coerce")
    due = due[due["Fecha vencimiento"].between(cutoff_ts, end_ts, inclusive="both")]
    if due.empty:
        st.info("No hay vencimientos previstos para los próximos 90 días.", icon=":material/event_available:")
        return

    due["Semana"] = due["Fecha vencimiento"] - pd.to_timedelta(due["Fecha vencimiento"].dt.weekday, unit="D")
    weekly = due.groupby("Semana", as_index=False).agg(
        Importe=("Importe", "sum"),
        Instrumentos=("Importe", "size"),
        Clientes=("Cliente", "nunique"),
    )
    weekly["Etiqueta"] = weekly["Instrumentos"].map(lambda value: f"{int(value)} chq.")
    dark_theme = st.context.theme.type == "dark"
    foreground = "#E7EEF8" if dark_theme else "#23364A"
    grid = "#2A3B53" if dark_theme else "#DCE5EE"
    accent = "#67B7F7" if dark_theme else "#245A8D"
    base = alt.Chart(weekly).encode(
        x=alt.X("Semana:T", title="Semana de vencimiento", axis=alt.Axis(format="%d %b", labelAngle=-25)),
        y=alt.Y("Importe:Q", title="Importe a vencer", axis=alt.Axis(format="$,.0s"), scale=alt.Scale(zero=True)),
    )
    bars = base.mark_bar(color=accent, cornerRadiusTopLeft=7, cornerRadiusTopRight=7, size=34).encode(
        tooltip=[
            alt.Tooltip("Semana:T", title="Semana", format="%d/%m/%Y"),
            alt.Tooltip("Importe:Q", title="Importe", format="$,.2f"),
            alt.Tooltip("Instrumentos:Q", title="Cheques", format=",.0f"),
            alt.Tooltip("Clientes:Q", title="Clientes", format=",.0f"),
        ]
    )
    labels = base.mark_text(color=foreground, dy=-10, fontSize=11, fontWeight=700).encode(text="Etiqueta:N")
    chart = (bars + labels).properties(height=315).configure_view(stroke=None).configure_axis(
        labelColor=foreground,
        titleColor=foreground,
        gridColor=grid,
        domainColor=grid,
        tickColor=grid,
        labelFontSize=11,
        titleFontSize=12,
    )
    st.altair_chart(chart, key="chart_due_flow")


def client_exposure_chart(data: pd.DataFrame) -> None:
    st.subheader("Exposición por cliente")
    st.caption("Ranking de importes todavía expuestos en cartera.")
    active_states = ["Pendiente", "Pendiente de acreditación", "Vencido", "Vence hoy"]
    exposure = data[data["Estado calculado"].isin(active_states)].copy()
    exposure["Cliente"] = exposure["Cliente"].fillna("").astype(str).str.strip().replace("", "Sin cliente")
    ranking = exposure.groupby("Cliente", as_index=False).agg(
        Importe=("Importe", "sum"),
        Instrumentos=("Importe", "size"),
    )
    ranking = ranking[ranking["Importe"] > 0].sort_values("Importe", ascending=False)
    if ranking.empty:
        st.info("No hay importes expuestos en la vista actual.", icon=":material/account_balance:")
        return

    total = float(ranking["Importe"].sum())
    ranking["Participación"] = ranking["Importe"] / total
    ranking["Etiqueta"] = ranking["Participación"].map(lambda value: f"{value:.0%}")
    visible = ranking.head(10).copy()
    dark_theme = st.context.theme.type == "dark"
    foreground = "#E7EEF8" if dark_theme else "#23364A"
    grid = "#2A3B53" if dark_theme else "#DCE5EE"
    accent = "#5DD39E" if dark_theme else "#2E8B70"
    ranking_max = float(visible["Importe"].max())
    base = alt.Chart(visible).encode(
        y=alt.Y(
            "Cliente:N",
            title=None,
            sort=alt.SortField(field="Importe", order="descending"),
            axis=alt.Axis(labelLimit=145),
        ),
        x=alt.X(
            "Importe:Q",
            title="Importe expuesto",
            axis=alt.Axis(format="$,.0s"),
            scale=alt.Scale(domain=[0, ranking_max * 1.22]),
        ),
    )
    bars = base.mark_bar(color=accent, cornerRadiusEnd=7, size=22).encode(
        tooltip=[
            alt.Tooltip("Cliente:N", title="Cliente"),
            alt.Tooltip("Importe:Q", title="Importe expuesto", format="$,.2f"),
            alt.Tooltip("Instrumentos:Q", title="Instrumentos", format=",.0f"),
            alt.Tooltip("Participación:Q", title="Participación", format=".1%"),
        ]
    )
    labels = base.mark_text(
        align="left",
        baseline="middle",
        color=foreground,
        dx=7,
        fontSize=11,
        fontWeight=700,
    ).encode(text="Etiqueta:N")
    chart = (bars + labels).properties(height=315).configure_view(stroke=None).configure_axis(
        labelColor=foreground,
        titleColor=foreground,
        gridColor=grid,
        domainColor=grid,
        tickColor=grid,
        labelFontSize=11,
        titleFontSize=12,
    )
    st.altair_chart(chart, key="chart_client_exposure")
    if len(ranking) > len(visible):
        st.caption("Se muestran los 10 clientes con mayor exposición.")


def receipt_coverage_chart(data: pd.DataFrame) -> None:
    summary = receipt_summary(data)
    st.subheader("Movimientos con y sin recibo")
    st.caption("Compara cantidad e importe de los movimientos incluidos en la vista actual.")
    if summary.empty or int(summary["Cantidad"].sum()) == 0:
        st.info("No hay movimientos para analizar en la vista actual.", icon=":material/receipt_long:")
        return

    dark_theme = st.context.theme.type == "dark"
    foreground = "#E7EEF8" if dark_theme else "#23364A"
    grid = "#2A3B53" if dark_theme else "#DCE5EE"
    colors = ["#5DD39E", "#FF8B86"] if dark_theme else ["#2E8B70", "#C95651"]
    color = alt.Color(
        "Vínculo:N",
        title=None,
        scale=alt.Scale(domain=["Con recibo", "Sin recibo"], range=colors),
        legend=alt.Legend(orient="bottom", direction="horizontal", symbolType="circle"),
    )

    count_base = alt.Chart(summary)
    count_arcs = count_base.mark_arc(innerRadius=64, outerRadius=112, padAngle=0.025, cornerRadius=5).encode(
        theta=alt.Theta("Cantidad:Q", stack=True),
        color=color,
        tooltip=[
            alt.Tooltip("Vínculo:N", title="Estado"),
            alt.Tooltip("Cantidad:Q", title="Cheques", format=",.0f"),
            alt.Tooltip("Participación cantidad:Q", title="Participación", format=".1%"),
            alt.Tooltip("Importe:Q", title="Importe", format="$,.2f"),
        ],
    )
    count_labels = count_base.mark_text(radius=88, color="#FFFFFF", fontSize=12, fontWeight=700).encode(
        theta=alt.Theta("Cantidad:Q", stack=True),
        text=alt.condition(
            alt.datum["Participación cantidad"] >= 0.08,
            alt.Text("Participación cantidad:Q", format=".0%"),
            alt.value(""),
        ),
    )
    total_count = f"{int(summary['Cantidad'].sum()):,}".replace(",", ".")
    count_center = alt.Chart(pd.DataFrame({"Total": [total_count]})).mark_text(
        color=foreground, fontSize=18, fontWeight=700
    ).encode(text="Total:N")
    count_chart = (count_arcs + count_labels + count_center).properties(height=300)

    amount_max = max(float(summary["Importe"].max()), 1.0)
    amount_base = alt.Chart(summary).encode(
        y=alt.Y("Vínculo:N", title=None, sort=["Con recibo", "Sin recibo"]),
        x=alt.X(
            "Importe:Q",
            title="Importe",
            axis=alt.Axis(format="$,.0s"),
            scale=alt.Scale(domain=[0, amount_max * 1.28]),
        ),
        color=color,
    )
    amount_bars = amount_base.mark_bar(cornerRadiusEnd=8, size=34).encode(
        tooltip=[
            alt.Tooltip("Vínculo:N", title="Estado"),
            alt.Tooltip("Cantidad:Q", title="Cheques", format=",.0f"),
            alt.Tooltip("Importe:Q", title="Importe", format="$,.2f"),
            alt.Tooltip("Importe promedio:Q", title="Promedio", format="$,.2f"),
            alt.Tooltip("Participación importe:Q", title="Participación", format=".1%"),
        ]
    )
    amount_labels = amount_base.mark_text(
        align="left", baseline="middle", dx=8, color=foreground, fontSize=12, fontWeight=700
    ).encode(text=alt.Text("Importe:Q", format="$,.0s"))
    amount_chart = (amount_bars + amount_labels).properties(height=300)

    count_column, amount_column = st.columns([0.9, 1.25])
    with count_column:
        st.markdown("**Cantidad de cheques**")
        st.altair_chart(
            count_chart.configure_view(stroke=None).configure_legend(labelColor=foreground, titleColor=foreground),
            key="chart_receipt_count",
        )
    with amount_column:
        st.markdown("**Importe asociado**")
        st.altair_chart(
            amount_chart.configure_view(stroke=None).configure_axis(
                labelColor=foreground, titleColor=foreground, gridColor=grid, domainColor=grid, tickColor=grid
            ).configure_legend(labelColor=foreground, titleColor=foreground),
            key="chart_receipt_amount",
        )
    with st.expander("Ver estadísticas de recibos", icon=":material/table_view:"):
        st.dataframe(
            summary,
            hide_index=True,
            width="stretch",
            column_config={
                "Cantidad": st.column_config.NumberColumn("Cheques", format="%d"),
                "Importe": st.column_config.NumberColumn(format="$ %.2f"),
                "Importe promedio": st.column_config.NumberColumn(format="$ %.2f"),
                "Participación cantidad": st.column_config.ProgressColumn("% cheques", format="percent"),
                "Participación importe": st.column_config.ProgressColumn("% importe", format="percent"),
            },
        )


def rejected_bank_chart(data: pd.DataFrame) -> None:
    summary = rejected_bank_summary(data)
    st.subheader("Rechazados por banco")
    st.caption("Estadísticas exclusivas de Banco Macro, Galicia y Nación.")
    if summary.empty:
        st.info("No hay cheques rechazados de estos tres bancos en la vista actual.", icon=":material/account_balance:")
        return

    indexed = summary.set_index("Banco")
    with st.container(horizontal=True):
        for bank in BANK_FILTER_OPTIONS:
            amount = float(indexed.at[bank, "Importe rechazado"]) if bank in indexed.index else 0.0
            count = int(indexed.at[bank, "Cantidad de rechazados"]) if bank in indexed.index else 0
            st.metric(f"{bank} · {count} rechazados", format_currency(amount), border=True)

    dark_theme = st.context.theme.type == "dark"
    foreground = "#E7EEF8" if dark_theme else "#23364A"
    grid = "#2A3B53" if dark_theme else "#DCE5EE"
    palette = ["#67B7F7", "#B99AF5", "#F8C65A"] if dark_theme else ["#245A8D", "#7957B8", "#C48717"]
    chart_data = summary.copy()
    chart_data["Etiqueta"] = chart_data.apply(
        lambda row: f"{format_currency(row['Importe rechazado'])} · {int(row['Cantidad de rechazados'])} chq.", axis=1
    )
    amount_max = max(float(chart_data["Importe rechazado"].max()), 1.0)
    base = alt.Chart(chart_data).encode(
        y=alt.Y("Banco:N", title=None, sort=list(BANK_FILTER_OPTIONS)),
        x=alt.X(
            "Importe rechazado:Q",
            title="Importe rechazado",
            axis=alt.Axis(format="$,.0s"),
            scale=alt.Scale(domain=[0, amount_max * 1.42]),
        ),
        color=alt.Color(
            "Banco:N",
            title=None,
            scale=alt.Scale(domain=list(BANK_FILTER_OPTIONS), range=palette),
            legend=None,
        ),
    )
    bars = base.mark_bar(cornerRadiusEnd=8, size=36).encode(
        tooltip=[
            alt.Tooltip("Banco:N", title="Banco"),
            alt.Tooltip("Cantidad de rechazados:Q", title="Rechazados", format=",.0f"),
            alt.Tooltip("Importe rechazado:Q", title="Importe", format="$,.2f"),
            alt.Tooltip("Importe promedio:Q", title="Promedio", format="$,.2f"),
            alt.Tooltip("Clientes afectados:Q", title="Clientes", format=",.0f"),
        ]
    )
    labels = base.mark_text(
        align="left", baseline="middle", dx=9, color=foreground, fontSize=12, fontWeight=700
    ).encode(text="Etiqueta:N")
    chart = (bars + labels).properties(height=260).configure_view(stroke=None).configure_axis(
        labelColor=foreground,
        titleColor=foreground,
        gridColor=grid,
        domainColor=grid,
        tickColor=grid,
        labelFontSize=12,
        titleFontSize=12,
    )
    st.altair_chart(chart, key="chart_rejected_banks")
    with st.expander("Ver estadísticas por banco", icon=":material/table_view:"):
        st.dataframe(
            summary,
            hide_index=True,
            width="stretch",
            column_config={
                "Cantidad de rechazados": st.column_config.NumberColumn(format="%d"),
                "Importe rechazado": st.column_config.NumberColumn(format="$ %.2f"),
                "Importe promedio": st.column_config.NumberColumn(format="$ %.2f"),
                "Clientes afectados": st.column_config.NumberColumn(format="%d"),
            },
        )


def rejected_trend_chart(data: pd.DataFrame) -> None:
    monthly = rejected_monthly_summary(data)
    st.subheader("Rechazados: cuándo y quiénes")
    st.caption("Las barras muestran importes; las etiquetas indican la cantidad de cheques rechazados.")
    if monthly.empty:
        st.info("No hay rechazos con fecha disponible en la vista actual.", icon=":material/info:")
        return

    month_totals = monthly.groupby("Mes", as_index=False).agg(
        **{
            "Cantidad de rechazados": ("Cantidad de rechazados", "sum"),
            "Importe rechazado": ("Importe rechazado", "sum"),
            "Clientes afectados": ("Cliente", "nunique"),
        }
    )
    month_totals["Etiqueta"] = month_totals["Cantidad de rechazados"].map(lambda value: f"{int(value)} rech.")
    client_totals = monthly.groupby("Cliente", as_index=False).agg(
        **{
            "Cantidad de rechazados": ("Cantidad de rechazados", "sum"),
            "Importe rechazado": ("Importe rechazado", "sum"),
            "Meses con rechazos": ("Mes", "nunique"),
        }
    )
    client_totals["Importe promedio"] = client_totals["Importe rechazado"] / client_totals["Cantidad de rechazados"]
    client_ranking = client_totals.nlargest(10, "Importe rechazado").sort_values("Importe rechazado")

    dark_theme = st.context.theme.type == "dark"
    foreground = "#E7EEF8" if dark_theme else "#23364A"
    grid = "#2A3B53" if dark_theme else "#DCE5EE"
    month_color = "#FF8B86" if dark_theme else "#C95651"
    client_color = "#67B7F7" if dark_theme else "#245A8D"

    month_base = alt.Chart(month_totals).encode(
        x=alt.X("Mes:T", title="Mes", axis=alt.Axis(format="%b %Y", labelAngle=-25, tickCount="month")),
        y=alt.Y(
            "Importe rechazado:Q",
            title="Importe rechazado",
            scale=alt.Scale(zero=True),
            axis=alt.Axis(format="$,.0s"),
        ),
    )
    month_bars = month_base.mark_bar(
        color=month_color,
        cornerRadiusTopLeft=7,
        cornerRadiusTopRight=7,
        size=42,
    ).encode(
        tooltip=[
            alt.Tooltip("Mes:T", title="Mes", format="%B %Y"),
            alt.Tooltip("Importe rechazado:Q", title="Importe", format="$,.2f"),
            alt.Tooltip("Cantidad de rechazados:Q", title="Cantidad", format=",.0f"),
            alt.Tooltip("Clientes afectados:Q", title="Clientes", format=",.0f"),
        ]
    )
    month_labels = month_base.mark_text(
        color=foreground,
        dy=-11,
        fontSize=12,
        fontWeight=700,
    ).encode(text=alt.Text("Etiqueta:N"))
    month_chart = (month_bars + month_labels).properties(height=330).configure_view(stroke=None).configure_axis(
        labelColor=foreground,
        titleColor=foreground,
        gridColor=grid,
        domainColor=grid,
        tickColor=grid,
        labelFontSize=12,
        titleFontSize=13,
    )

    ranking_max = float(client_ranking["Importe rechazado"].max())
    client_base = alt.Chart(client_ranking).encode(
        y=alt.Y(
            "Cliente:N",
            title=None,
            sort=alt.SortField(field="Importe rechazado", order="descending"),
            axis=alt.Axis(labelLimit=160),
        ),
        x=alt.X(
            "Importe rechazado:Q",
            title="Importe rechazado",
            scale=alt.Scale(domain=[0, ranking_max * 1.28]),
            axis=alt.Axis(format="$,.0s"),
        ),
    )
    client_bars = client_base.mark_bar(
        color=client_color,
        cornerRadiusEnd=7,
        height=23,
    ).encode(
        tooltip=[
            alt.Tooltip("Cliente:N", title="Cliente"),
            alt.Tooltip("Cantidad de rechazados:Q", title="Cantidad", format=",.0f"),
            alt.Tooltip("Importe rechazado:Q", title="Importe", format="$,.2f"),
            alt.Tooltip("Importe promedio:Q", title="Promedio", format="$,.2f"),
            alt.Tooltip("Meses con rechazos:Q", title="Meses afectados", format=",.0f"),
        ]
    )
    client_labels = client_base.mark_text(
        align="left",
        baseline="middle",
        color=foreground,
        dx=8,
        fontSize=11,
        fontWeight=700,
    ).encode(text=alt.Text("Importe rechazado:Q", format="$,.0s"))
    client_chart = (client_bars + client_labels).properties(height=330).configure_view(stroke=None).configure_axis(
        labelColor=foreground,
        titleColor=foreground,
        gridColor=grid,
        domainColor=grid,
        tickColor=grid,
        labelFontSize=12,
        titleFontSize=13,
    )

    with st.container(horizontal=True):
        st.metric("Rechazos", int(monthly["Cantidad de rechazados"].sum()), border=True)
        st.metric("Importe rechazado", format_currency(monthly["Importe rechazado"].sum()), border=True)
        st.metric("Clientes afectados", monthly["Cliente"].nunique(), border=True)
        st.metric("Meses con rechazos", monthly["Mes"].nunique(), border=True)
    month_column, client_column = st.columns([1.15, 1])
    with month_column:
        st.markdown("**Importe y cantidad por mes**")
        st.altair_chart(month_chart, key="chart_rejected_month")
    with client_column:
        st.markdown("**Clientes con mayor importe rechazado**")
        st.altair_chart(client_chart, key="chart_rejected_clients")
    if len(client_totals) > len(client_ranking):
        st.caption("El ranking muestra los 10 clientes con mayor importe; el detalle incluye todos.")
    with st.expander("Ver detalle mensual", icon=":material/table_view:"):
        st.dataframe(
            monthly.sort_values(["Mes", "Importe rechazado"], ascending=[False, False]),
            hide_index=True,
            width="stretch",
            column_config={
                "Mes": st.column_config.DateColumn(format="MM/YYYY"),
                "Cliente": st.column_config.TextColumn(pinned=True),
                "Cantidad de rechazados": st.column_config.NumberColumn(format="%d"),
                "Importe rechazado": st.column_config.NumberColumn(format="$ %.2f"),
                "Importe promedio": st.column_config.NumberColumn(format="$ %.2f"),
            },
        )
    st.caption("Mes según fecha de ingreso/pago; si falta, se usa vencimiento y luego acreditación.")


require_access()
with st.container(horizontal=True, vertical_alignment="center"):
    st.title("Cartera de cheques")
    st.badge("Procesamiento privado", icon=":material/shield_lock:", color="blue")
st.caption(
    "Cargá el CONRENPF para obtener la cartera operativa. El Excel se procesa en memoria y no queda guardado en la aplicación."
)

with st.sidebar:
    st.header("Panel de control")
    st.caption(":material/contrast: Tema claro u oscuro desde el menú ⋮.")
    with st.expander("Archivo y fecha de corte", icon=":material/upload_file:", expanded=True):
        uploaded = st.file_uploader(
            "Archivo del concentrador",
            type=["xlsx", "xlsm"],
            max_upload_size=15,
            help="Excel original del CONRENPF. Máximo 15 MB.",
            key="source_file",
        )
        cutoff = st.date_input(
            "Fecha de corte / acreditación",
            value=date.today(),
            format="DD/MM/YYYY",
            help="Se considera acreditado todo cheque cuya fecha de acreditación sea igual o anterior a este corte.",
        )
        st.caption("La cartera se recalcula completa al mover esta fecha.")
        if uploaded is not None and st.button("Descartar archivo", icon=":material/delete:", width="stretch"):
            st.cache_data.clear(); st.session_state.pop("source_file", None); st.rerun()

if uploaded is None:
    with st.container(border=True):
        st.subheader("Empezar")
        st.write("Seleccioná el archivo del concentrador desde el panel lateral.")
        st.info(
            "Incluye CH24, CH48, CPD, ECHEQ y ECHEQDIF. No modifica ni guarda el Excel original.",
            icon=":material/info:",
        )
    st.stop()

file_bytes = uploaded.getvalue()
file_digest = hashlib.sha256(file_bytes).hexdigest()
file_fingerprint = file_digest[:12]
LOGGER.info("Procesando archivo id=%s bytes=%d", file_fingerprint, len(file_bytes))
try:
    portfolio, raw = process_file(file_bytes, cutoff, PROCESSING_RULE_VERSION)
except ConcentradorError as error:
    st.error(str(error), icon=":material/error:"); st.stop()
except Exception:
    LOGGER.exception("Fallo inesperado al procesar archivo id=%s", file_fingerprint)
    st.error("No pude procesar el archivo. Confirmá que sea un CONRENPF válido y no protegido.", icon=":material/error:"); st.stop()
finally:
    del file_bytes

if portfolio.empty:
    st.warning("El archivo no contiene instrumentos compatibles."); st.stop()

with st.sidebar:
    with st.expander("Estado de la cartera", icon=":material/account_balance_wallet:", expanded=True):
        scope = st.selectbox(
            "Vista",
            ["Todos", "En cartera", "Pend. acreditación", "Rechazados", "Acreditados"],
            key="portfolio_scope",
        )
    with st.expander("Comprobantes", icon=":material/receipt_long:", expanded=False):
        if scope == "En cartera":
            receipt_scope = "Con comprobante asociado"
            st.info("En cartera muestra todos los cheques con recibo, estén acreditados o no.", icon=":material/receipt_long:")
        else:
            receipt_scope = st.selectbox(
                "Comprobante asociado",
                ["Todos", "Con comprobante asociado", "Sin comprobante asociado"],
                key="receipt_filter",
            )
    with st.expander("Filtros operativos", icon=":material/filter_alt:", expanded=False):
        selected_types = st.pills("Tipos", list(ALLOWED_TYPES), default=list(ALLOWED_TYPES), selection_mode="multi")
        selected_clients = st.multiselect("Clientes", sorted(x for x in portfolio["Cliente"].dropna().unique() if x), placeholder="Todos")
        selected_banks = st.pills(
            "Bancos",
            list(BANK_FILTER_OPTIONS),
            selection_mode="multi",
            help="Filtro operativo limitado a Banco Macro, Galicia y Nación.",
        )
        search = st.text_input("Buscar cheque, CUIT o recibo", placeholder="Número o texto")

filtered = apply_operational_scope(portfolio, scope)
if receipt_scope == "Con comprobante asociado": filtered = filtered[filtered["Estado recibo"].eq("Tomado")]
elif receipt_scope == "Sin comprobante asociado": filtered = filtered[filtered["Estado recibo"].eq("Sin recibo asociado")]
filtered = filtered[filtered["Tipo"].isin(selected_types)] if selected_types else filtered.iloc[0:0]
if selected_clients: filtered = filtered[filtered["Cliente"].isin(selected_clients)]
if selected_banks:
    filtered = filtered[filtered["Banco cheque"].map(bank_filter_group).isin(selected_banks)]
if search.strip():
    needle = search.strip().casefold()
    searchable = filtered[["Cliente", "CUIT cliente", "N° cheque / eCheq", "Recibo relacionado"]].fillna("").astype(str).agg(" ".join, axis=1).str.casefold()
    filtered = filtered[searchable.str.contains(needle, regex=False)]

in_portfolio = filtered[filtered["Estado recibo"].eq("Tomado")]
rejected = filtered[filtered["Estado calculado"].eq("Rechazado")]
next_7 = filtered[filtered["Estado calculado"].isin(["Pendiente", "Vence hoy"]) & filtered["Días al vencimiento"].between(0, 7, inclusive="both")]
with st.container(horizontal=True):
    st.metric("Importe total", format_currency(filtered["Importe"].sum()), border=True)
    st.metric("En cartera", format_currency(in_portfolio["Importe"].sum()), border=True)
    st.metric("Próximos 7 días", format_currency(next_7["Importe"].sum()), border=True)
    st.metric("Rechazados", format_currency(rejected["Importe"].sum()), border=True)
with_receipt = filtered[filtered["Estado recibo"].eq("Tomado")]
without_receipt = filtered[filtered["Estado recibo"].ne("Tomado")]
total_count = len(filtered)
receipt_coverage = len(with_receipt) / total_count if total_count else 0.0
amount_total = float(filtered["Importe"].sum())
receipt_amount_share = float(with_receipt["Importe"].sum()) / amount_total if amount_total else 0.0
with st.container(horizontal=True):
    st.metric(f"Total · {total_count:,} cheques".replace(",", "."), format_currency(amount_total), border=True)
    st.metric(f"Con recibo · {len(with_receipt):,} cheques".replace(",", "."), format_currency(with_receipt["Importe"].sum()), border=True)
    st.metric(f"Sin recibo · {len(without_receipt):,} cheques".replace(",", "."), format_currency(without_receipt["Importe"].sum()), border=True)
    st.metric(f"Cobertura · {receipt_amount_share:.1%} del importe", f"{receipt_coverage:.1%}", border=True)
st.caption(f":material/data_check: {len(filtered):,} instrumentos incluidos en la vista actual.".replace(",", "."))
st.caption(f":material/event_available: Acreditación calculada al **{cutoff:%d/%m/%Y}**.")
if scope == "Pend. acreditación":
    ps_in_file = int(portfolio["Código estado"].eq("PS").sum())
    pending_at_cutoff = int(portfolio["Estado calculado"].eq("Pendiente de acreditación").sum())
    st.caption(
        f":material/pending_actions: Pendientes según el corte: **{pending_at_cutoff:,}** · "
        f"código PS en el archivo: **{ps_in_file:,}** · visibles con los demás filtros: **{len(filtered):,}**".replace(",", ".")
    )

view = st.segmented_control(
    "Planilla",
    ["Resumen ejecutivo", "Detalle", "Calidad de vínculos", "Reportes"],
    default="Resumen ejecutivo",
)
if view == "Resumen ejecutivo":
    left, right = st.columns(2)
    with left, st.container(border=True, height="stretch"):
        donut_chart(filtered, "Tipo", "Importe por tipo", "chart_type")
    with right, st.container(border=True, height="stretch"):
        donut_chart(filtered, "Estado calculado", "Importe por estado", "chart_state")
    with st.container(border=True):
        receipt_coverage_chart(filtered)
    left, right = st.columns(2)
    with left, st.container(border=True, height="stretch"):
        due_flow_chart(filtered, cutoff)
    with right, st.container(border=True, height="stretch"):
        client_exposure_chart(filtered)
    with st.container(border=True):
        rejected_trend_chart(filtered)
    with st.container(border=True):
        rejected_bank_chart(filtered)
    st.subheader("Próximos vencimientos")
    next_due = filtered[filtered["Estado calculado"].isin(["Pendiente", "Vence hoy"]) & filtered["Fecha vencimiento"].between(pd.Timestamp(cutoff), pd.Timestamp(cutoff + timedelta(days=30)), inclusive="both")]
    st.dataframe(next_due.sort_values(["Fecha vencimiento", "Importe"], ascending=[True, False])[["Fecha vencimiento", "Cliente", "Tipo", "N° cheque / eCheq", "Importe", "Estado calculado"]].head(25), hide_index=True,
        column_config={"Fecha vencimiento": st.column_config.DateColumn("Vencimiento", format="DD/MM/YYYY"), "Importe": st.column_config.NumberColumn(format="$ %.2f")}, height=360)
elif view == "Detalle":
    st.caption(f"Mostrando {len(filtered):,} de {len(portfolio):,} instrumentos".replace(",", "."))
    columns = ["Tipo", "Cliente", "CUIT cliente", "N° cheque / eCheq", "Banco cheque", "Importe", "Fecha ingreso / pago", "Fecha acreditación", "Fecha vencimiento", "Días al vencimiento", "Código estado", "Estado original", "Estado calculado", "Estado recibo", "Recibo relacionado", "Fuente del vínculo", "Nro Cpb Relación", "Observaciones", "Código rechazo", "Motivo rechazo", "Alertas", "Fila fuente"]
    st.dataframe(filtered[columns], hide_index=True, column_config={"Cliente": st.column_config.TextColumn(pinned=True), "Importe": st.column_config.NumberColumn(format="$ %.2f"), "Fecha ingreso / pago": st.column_config.DateColumn(format="DD/MM/YYYY"), "Fecha acreditación": st.column_config.DateColumn(format="DD/MM/YYYY"), "Fecha vencimiento": st.column_config.DateColumn(format="DD/MM/YYYY")}, height=620)
elif view == "Calidad de vínculos":
    linked = filtered[filtered["Recibo relacionado"].ne("")]
    missing = filtered[filtered["Recibo relacionado"].eq("")]
    with st.container(horizontal=True):
        st.metric("Tomados", len(linked), border=True)
        st.metric("Sin recibo asociado", len(missing), border=True)
        st.metric("Desde observación", len(linked[linked["Fuente del vínculo"].eq("Observación")]), border=True)
        st.metric("Desde comprobante", len(linked[linked["Fuente del vínculo"].eq("Nro Cpb Relación")]), border=True)
    left, right = st.columns([1, 1.25])
    with left, st.container(border=True, height="stretch"):
        donut_chart(linked, "Fuente del vínculo", "Origen de los recibos", "chart_receipt_source", max_slices=4)
    with right, st.container(border=True, height="stretch"):
        st.subheader("Instrumentos sin recibo asociado")
        if missing.empty:
            st.success("Todos los instrumentos tienen un recibo vinculado.", icon=":material/check_circle:")
        else:
            st.dataframe(
                missing[["Cliente", "Tipo", "N° cheque / eCheq", "Nro Cpb Relación", "Observaciones", "Fila fuente"]],
                hide_index=True,
                height=430,
            )
else:
    st.subheader("Centro de reportes")
    st.caption("Documentos preparados a partir del archivo procesado, sin modificar el original.")
    pdf_column, excel_column = st.columns(2)
    with pdf_column, st.container(border=True, height="stretch"):
        st.markdown("### :material/picture_as_pdf: Cartera completa en PDF")
        st.write("Portada ejecutiva, composición por estado y detalle de todos los instrumentos.")
        st.caption(f"Incluye {len(portfolio):,} movimientos, sin aplicar los filtros de pantalla.".replace(",", "."))
        pdf_report = make_pdf(portfolio, cutoff)
        st.download_button(
            "Descargar PDF completo",
            data=pdf_report,
            file_name=f"cartera_completa_{cutoff:%Y-%m-%d}.pdf",
            mime="application/pdf",
            icon=":material/download:",
            type="primary",
            width="stretch",
        )
    with excel_column, st.container(border=True, height="stretch"):
        st.markdown("### :material/table_view: Vista filtrada en Excel")
        st.write("Planilla operativa con los filtros actuales y las filas fuente relacionadas.")
        st.caption(f"Incluye {len(filtered):,} de {len(portfolio):,} movimientos.".replace(",", "."))
        excel_report = make_excel(filtered, raw, cutoff)
        st.download_button(
            "Descargar Excel filtrado",
            data=excel_report,
            file_name=f"cartera_filtrada_{cutoff:%Y-%m-%d}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            icon=":material/download:",
            width="stretch",
        )
    st.info(
        "El PDF contiene la cartera completa. El Excel respeta la vista, comprobantes, tipos, clientes, bancos y búsqueda seleccionados.",
        icon=":material/info:",
    )
