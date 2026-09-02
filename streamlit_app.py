from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from datetime import date

import altair as alt
import pandas as pd
import streamlit as st

from utils.analytics import (
    apply_operational_scope,
    collection_calendar_summary,
    pending_collection,
    pending_for_month,
    receipt_summary,
    rejected_bank_summary,
)
from utils.movements import (
    CONTROL_RECORD_TYPES,
    MOVEMENT_LINK_STATES,
    RECONCILIATION_STATES,
    build_movement_control,
    export_movements_excel,
    movement_link_summary,
    movement_type_summary,
    receipt_reconciliation,
)
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
PROCESSING_RULE_VERSION = "2026-09-02-unified-cheques-movements-v9"
BANK_FILTER_OPTIONS = ("Macro", "Galicia", "Nación")
MONTH_NAMES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre")


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

st.set_page_config(page_title="Control de cobranzas", page_icon=":material/account_balance_wallet:", layout="wide")


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

    st.title("Control de cobranzas")
    st.caption("Ingresá con un usuario autorizado para acceder al CONRENPF.")
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
        st.title("Control de cobranzas")
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
    portfolio, raw = portfolio_from_bytes(file_bytes, cutoff)
    return portfolio, build_movement_control(raw, portfolio), raw


@st.cache_data(ttl="5m", max_entries=2, show_spinner="Preparando el Excel…", scope="session")
def make_excel(portfolio: pd.DataFrame, raw: pd.DataFrame, cutoff: date) -> bytes:
    return export_excel(portfolio, raw, cutoff)


@st.cache_data(ttl="5m", max_entries=4, show_spinner="Preparando el Excel de movimientos…", scope="session")
def make_movements_excel(movements: pd.DataFrame, raw: pd.DataFrame, cutoff: date) -> bytes:
    return export_movements_excel(movements, raw, cutoff)


@st.cache_data(ttl="10m", max_entries=2, show_spinner="Generando el PDF profesional…", scope="session")
def make_pdf(portfolio: pd.DataFrame, cutoff: date) -> bytes:
    return export_portfolio_pdf(portfolio, cutoff)


def donut_chart(
    data: pd.DataFrame,
    category: str,
    title: str,
    key: str,
    max_slices: int = 7,
    description: str | None = None,
) -> None:
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
    if description:
        st.caption(description)
    st.altair_chart(chart, key=key)


def monthly_collection_chart(data: pd.DataFrame, cutoff: date) -> None:
    st.subheader("Cuándo debería entrar el dinero este mes")
    st.caption(
        "Cada barra muestra el importe todavía pendiente para una fecha. Se usa la fecha de acreditación y, si falta, el vencimiento."
    )
    calendar = collection_calendar_summary(data, cutoff)
    if calendar.empty:
        st.info("No hay cobros pendientes con fecha prevista dentro del mes analizado.", icon=":material/event_available:")
        return

    calendar["Etiqueta"] = calendar["Cantidad"].map(lambda value: f"{int(value)} chq.")
    dark_theme = st.context.theme.type == "dark"
    foreground = "#E7EEF8" if dark_theme else "#23364A"
    grid = "#2A3B53" if dark_theme else "#DCE5EE"
    colors = ["#67B7F7", "#FF8B86"] if dark_theme else ["#245A8D", "#C95651"]
    base = alt.Chart(calendar).encode(
        x=alt.X(
            "Fecha prevista de cobro:T",
            title="Fecha prevista de cobro",
            axis=alt.Axis(format="%d/%m", labelAngle=-35),
        ),
        y=alt.Y(
            "Importe:Q",
            title="Importe pendiente",
            axis=alt.Axis(format="$,.0s"),
            scale=alt.Scale(zero=True),
        ),
        color=alt.Color(
            "Situación:N",
            title=None,
            scale=alt.Scale(domain=["Próximo cobro", "Fecha ya cumplida"], range=colors),
            legend=alt.Legend(orient="bottom", direction="horizontal", symbolType="circle"),
        ),
    )
    bars = base.mark_bar(cornerRadiusTopLeft=7, cornerRadiusTopRight=7, size=32).encode(
        tooltip=[
            alt.Tooltip("Fecha prevista de cobro:T", title="Cobro previsto", format="%d/%m/%Y"),
            alt.Tooltip("Situación:N", title="Situación"),
            alt.Tooltip("Importe:Q", title="Importe pendiente", format="$,.2f"),
            alt.Tooltip("Cantidad:Q", title="Cheques", format=",.0f"),
            alt.Tooltip("Clientes:Q", title="Clientes", format=",.0f"),
        ]
    )
    labels = base.mark_text(color=foreground, dy=-10, fontSize=11, fontWeight=700).encode(text="Etiqueta:N")
    cutoff_rule = alt.Chart(pd.DataFrame({"Fecha": [pd.Timestamp(cutoff)]})).mark_rule(
        color=foreground, strokeDash=[5, 4], opacity=0.55
    ).encode(x="Fecha:T", tooltip=[alt.Tooltip("Fecha:T", title="Fecha de análisis", format="%d/%m/%Y")])
    chart = (bars + labels + cutoff_rule).properties(height=340).configure_view(stroke=None).configure_axis(
        labelColor=foreground,
        titleColor=foreground,
        gridColor=grid,
        domainColor=grid,
        tickColor=grid,
        labelFontSize=11,
        titleFontSize=12,
    ).configure_legend(labelColor=foreground, titleColor=foreground)
    st.altair_chart(chart, key="chart_due_flow")


def client_exposure_chart(data: pd.DataFrame) -> None:
    st.subheader("Clientes con mayor saldo pendiente")
    st.caption("Muestra de quién depende la mayor parte del dinero que todavía falta cobrar.")
    active_states = ["Pendiente", "Pendiente de acreditación", "Vencido", "Vence hoy"]
    exposure = data[data["Estado calculado"].isin(active_states)].copy()
    exposure["Cliente"] = exposure["Cliente"].fillna("").astype(str).str.strip().replace("", "Sin cliente")
    ranking = exposure.groupby("Cliente", as_index=False).agg(
        Importe=("Importe", "sum"),
        Instrumentos=("Importe", "size"),
    )
    ranking = ranking[ranking["Importe"] > 0].sort_values("Importe", ascending=False)
    if ranking.empty:
        st.info("No hay importes pendientes para comparar en la vista actual.", icon=":material/account_balance:")
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
            title="Importe pendiente de cobro",
            axis=alt.Axis(format="$,.0s"),
            scale=alt.Scale(domain=[0, ranking_max * 1.22]),
        ),
    )
    bars = base.mark_bar(color=accent, cornerRadiusEnd=7, size=22).encode(
        tooltip=[
            alt.Tooltip("Cliente:N", title="Cliente"),
            alt.Tooltip("Importe:Q", title="Importe pendiente", format="$,.2f"),
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
        st.caption("Se muestran los 10 clientes con mayor saldo pendiente.")


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


def rescued_client_chart(data: pd.DataFrame) -> None:
    rescued = data[data["Estado calculado"].eq("Rescatado")].copy()
    st.subheader("Cheques rescatados (RE)")
    st.caption("Son cheques retirados o recuperados. No se consideran rechazados ni pendientes de cobro.")
    if rescued.empty:
        st.info("No hay cheques rescatados en los filtros actuales.", icon=":material/check_circle:")
        return

    rescued["Cliente"] = rescued["Cliente"].fillna("").astype(str).str.strip().replace("", "Sin cliente")
    rescued["Importe"] = pd.to_numeric(rescued["Importe"], errors="coerce").fillna(0)
    ranking = rescued.groupby("Cliente", as_index=False).agg(
        Importe=("Importe", "sum"),
        Cheques=("Importe", "size"),
    ).sort_values("Importe", ascending=False)
    visible = ranking.head(10).sort_values("Importe")
    with st.container(horizontal=True):
        st.metric("Cheques rescatados", f"{len(rescued):,}".replace(",", "."), border=True)
        st.metric("Importe rescatado", format_currency(rescued["Importe"].sum()), border=True)
        st.metric("Clientes involucrados", f"{rescued['Cliente'].nunique():,}".replace(",", "."), border=True)

    dark_theme = st.context.theme.type == "dark"
    foreground = "#E7EEF8" if dark_theme else "#23364A"
    grid = "#2A3B53" if dark_theme else "#DCE5EE"
    accent = "#F8C65A" if dark_theme else "#C48717"
    maximum = max(float(visible["Importe"].max()), 1.0)
    base = alt.Chart(visible).encode(
        y=alt.Y("Cliente:N", title=None, sort=alt.SortField(field="Importe", order="descending")),
        x=alt.X(
            "Importe:Q",
            title="Importe rescatado",
            axis=alt.Axis(format="$,.0s"),
            scale=alt.Scale(domain=[0, maximum * 1.25]),
        ),
    )
    bars = base.mark_bar(color=accent, cornerRadiusEnd=7, size=24).encode(
        tooltip=[
            alt.Tooltip("Cliente:N", title="Cliente"),
            alt.Tooltip("Cheques:Q", title="Cheques rescatados", format=",.0f"),
            alt.Tooltip("Importe:Q", title="Importe rescatado", format="$,.2f"),
        ]
    )
    labels = base.mark_text(
        align="left", baseline="middle", dx=8, color=foreground, fontSize=11, fontWeight=700
    ).encode(text=alt.Text("Importe:Q", format="$,.0s"))
    chart = (bars + labels).properties(height=max(230, len(visible) * 34)).configure_view(stroke=None).configure_axis(
        labelColor=foreground, titleColor=foreground, gridColor=grid, domainColor=grid, tickColor=grid
    )
    st.altair_chart(chart, key="chart_rescued_clients")


def rejected_bank_chart(data: pd.DataFrame) -> None:
    summary = rejected_bank_summary(data)
    st.subheader("Cheques rechazados por banco girado")
    st.caption(
        "RC identifica un rechazo. Si el estado viene vacío, la app exige código y motivo compatibles; RE siempre permanece como rescatado."
    )
    if summary.empty:
        st.info("No hay cheques rechazados en la vista actual.", icon=":material/account_balance:")
        return

    indexed = summary.set_index("Banco")
    rejected_rows = data[data["Estado calculado"].eq("Rechazado")]
    with st.container(horizontal=True):
        st.metric(
            f"Total · {len(rejected_rows)} rechazados",
            format_currency(rejected_rows["Importe"].sum()),
            border=True,
        )
        for bank in BANK_FILTER_OPTIONS:
            amount = float(indexed.at[bank, "Importe rechazado"]) if bank in indexed.index else 0.0
            count = int(indexed.at[bank, "Cantidad de rechazados"]) if bank in indexed.index else 0
            st.metric(f"{bank} · {count} rechazados", format_currency(amount), border=True)

    other_count = int(indexed.at["Otros bancos", "Cantidad de rechazados"]) if "Otros bancos" in indexed.index else 0
    other_amount = float(indexed.at["Otros bancos", "Importe rechazado"]) if "Otros bancos" in indexed.index else 0.0
    if other_count:
        st.caption(
            f"Además hay **{other_count} rechazados** por **{format_currency(other_amount)}** de otros bancos; "
            "se conservan para que el total general no pierda movimientos."
        )

    dark_theme = st.context.theme.type == "dark"
    foreground = "#E7EEF8" if dark_theme else "#23364A"
    grid = "#2A3B53" if dark_theme else "#DCE5EE"
    display_order = [*BANK_FILTER_OPTIONS, "Otros bancos"]
    palette = ["#67B7F7", "#B99AF5", "#F8C65A", "#9AA9BC"] if dark_theme else ["#245A8D", "#7957B8", "#C48717", "#65758B"]
    chart_data = summary.copy()
    chart_data["Etiqueta"] = chart_data.apply(
        lambda row: f"{format_currency(row['Importe rechazado'])} · {int(row['Cantidad de rechazados'])} chq.", axis=1
    )
    amount_max = max(float(chart_data["Importe rechazado"].max()), 1.0)
    base = alt.Chart(chart_data).encode(
        y=alt.Y("Banco:N", title=None, sort=display_order),
        x=alt.X(
            "Importe rechazado:Q",
            title="Importe rechazado",
            axis=alt.Axis(format="$,.0s"),
            scale=alt.Scale(domain=[0, amount_max * 1.42]),
        ),
        color=alt.Color(
            "Banco:N",
            title=None,
            scale=alt.Scale(domain=display_order, range=palette),
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
    st.subheader("Rechazados (RC): evolución y clientes")
    st.caption("Muestra cuándo ocurrieron los rechazos y qué clientes concentran el mayor importe.")
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


def movement_link_chart(data: pd.DataFrame) -> None:
    st.subheader("Cobertura de recibos")
    st.caption("Compara cheques y movimientos bancarios con recibo, sin recibo o con fuentes incompatibles.")
    summary = movement_link_summary(data)
    if summary.empty or int(summary["Cantidad"].sum()) == 0:
        st.info("No hay movimientos para representar.", icon=":material/receipt_long:")
        return
    summary["Etiqueta"] = summary.apply(
        lambda row: f"{format_currency(row['Importe'])} · {int(row['Cantidad'])} reg.", axis=1
    )
    dark_theme = st.context.theme.type == "dark"
    foreground = "#E7EEF8" if dark_theme else "#23364A"
    grid = "#2A3B53" if dark_theme else "#DCE5EE"
    palette = ["#5DD39E", "#FF8B86", "#F8C65A"] if dark_theme else ["#2E8B70", "#C95651", "#C48717"]
    amount_max = max(float(summary["Importe"].max()), 1.0)
    base = alt.Chart(summary).encode(
        y=alt.Y("Estado vínculo:N", title=None, sort=list(MOVEMENT_LINK_STATES)),
        x=alt.X(
            "Importe:Q",
            title="Importe controlado",
            axis=alt.Axis(format="$,.0s"),
            scale=alt.Scale(domain=[0, amount_max * 1.35]),
        ),
        color=alt.Color(
            "Estado vínculo:N",
            title=None,
            scale=alt.Scale(domain=list(MOVEMENT_LINK_STATES), range=palette),
            legend=None,
        ),
    )
    bars = base.mark_bar(cornerRadiusEnd=8, size=34).encode(
        tooltip=[
            alt.Tooltip("Estado vínculo:N", title="Estado"),
            alt.Tooltip("Cantidad:Q", title="Registros", format=",.0f"),
            alt.Tooltip("Importe:Q", title="Importe", format="$,.2f"),
            alt.Tooltip("Participación importe:Q", title="Participación", format=".1%"),
        ]
    )
    labels = base.mark_text(
        align="left", baseline="middle", dx=8, color=foreground, fontSize=11, fontWeight=700
    ).encode(text="Etiqueta:N")
    chart = (bars + labels).properties(height=250).configure_view(stroke=None).configure_axis(
        labelColor=foreground,
        titleColor=foreground,
        gridColor=grid,
        domainColor=grid,
        tickColor=grid,
    )
    st.altair_chart(chart, key="movement_link_chart")


def movement_type_chart(data: pd.DataFrame) -> None:
    st.subheader("Cartera vs. movimientos bancarios")
    st.caption("Comparación bruta del importe del período; no implica una coincidencia automática entre registros.")
    summary = movement_type_summary(data)
    if summary.empty or int(summary["Cantidad"].sum()) == 0:
        st.info("No hay registros para comparar.", icon=":material/compare_arrows:")
        return
    summary["Etiqueta"] = summary.apply(
        lambda row: f"{format_currency(row['Importe'])} · {int(row['Cantidad'])} reg.", axis=1
    )
    dark_theme = st.context.theme.type == "dark"
    foreground = "#E7EEF8" if dark_theme else "#23364A"
    grid = "#2A3B53" if dark_theme else "#DCE5EE"
    palette = ["#67B7F7", "#5DD39E"] if dark_theme else ["#245A8D", "#2E8B70"]
    amount_max = max(float(summary["Importe"].max()), 1.0)
    base = alt.Chart(summary).encode(
        y=alt.Y("Tipo de registro:N", title=None, sort=list(CONTROL_RECORD_TYPES)),
        x=alt.X(
            "Importe:Q",
            title="Importe",
            axis=alt.Axis(format="$,.0s"),
            scale=alt.Scale(domain=[0, amount_max * 1.35]),
        ),
        color=alt.Color(
            "Tipo de registro:N",
            title=None,
            scale=alt.Scale(domain=list(CONTROL_RECORD_TYPES), range=palette),
            legend=None,
        ),
    )
    bars = base.mark_bar(cornerRadiusEnd=8, size=34).encode(
        tooltip=[
            alt.Tooltip("Tipo de registro:N", title="Registro"),
            alt.Tooltip("Cantidad:Q", title="Cantidad", format=",.0f"),
            alt.Tooltip("Importe:Q", title="Importe", format="$,.2f"),
            alt.Tooltip("Participación importe:Q", title="Participación", format=".1%"),
        ]
    )
    labels = base.mark_text(
        align="left", baseline="middle", dx=8, color=foreground, fontSize=11, fontWeight=700
    ).encode(text="Etiqueta:N")
    chart = (bars + labels).properties(height=250).configure_view(stroke=None).configure_axis(
        labelColor=foreground,
        titleColor=foreground,
        gridColor=grid,
        domainColor=grid,
        tickColor=grid,
    )
    st.altair_chart(chart, key="movement_type_chart")


def movement_method_chart(data: pd.DataFrame) -> None:
    st.subheader("Composición por medio de pago")
    st.caption("Muestra cuánto representan cheques, transferencias, efectivo, depósitos y los demás medios.")
    if data.empty:
        st.info("No hay movimientos para comparar.", icon=":material/account_balance:")
        return
    grouped = data.copy()
    grouped["Grupo medio de pago"] = grouped["Grupo medio de pago"].fillna("").replace("", "Otro")
    grouped["Importe"] = pd.to_numeric(grouped["Importe"], errors="coerce").fillna(0)
    grouped = grouped.groupby("Grupo medio de pago", as_index=False).agg(
        Importe=("Importe", "sum"), Movimientos=("Importe", "size")
    ).sort_values("Importe", ascending=False).head(12)
    grouped["Etiqueta"] = grouped["Movimientos"].map(lambda value: f"{int(value)} mov.")
    dark_theme = st.context.theme.type == "dark"
    foreground = "#E7EEF8" if dark_theme else "#23364A"
    grid = "#2A3B53" if dark_theme else "#DCE5EE"
    accent = "#67B7F7" if dark_theme else "#245A8D"
    amount_max = max(float(grouped["Importe"].max()), 1.0)
    base = alt.Chart(grouped).encode(
        y=alt.Y(
            "Grupo medio de pago:N",
            title=None,
            sort=alt.SortField(field="Importe", order="descending"),
            axis=alt.Axis(labelLimit=165),
        ),
        x=alt.X(
            "Importe:Q",
            title="Importe",
            axis=alt.Axis(format="$,.0s"),
            scale=alt.Scale(domain=[0, amount_max * 1.2]),
        ),
    )
    bars = base.mark_bar(color=accent, cornerRadiusEnd=8, size=25).encode(
        tooltip=[
            alt.Tooltip("Grupo medio de pago:N", title="Medio"),
            alt.Tooltip("Movimientos:Q", title="Movimientos", format=",.0f"),
            alt.Tooltip("Importe:Q", title="Importe", format="$,.2f"),
        ]
    )
    labels = base.mark_text(
        align="left", baseline="middle", dx=7, color=foreground, fontSize=11, fontWeight=700
    ).encode(text="Etiqueta:N")
    chart = (bars + labels).properties(height=250).configure_view(stroke=None).configure_axis(
        labelColor=foreground,
        titleColor=foreground,
        gridColor=grid,
        domainColor=grid,
        tickColor=grid,
    )
    st.altair_chart(chart, key="movement_method_chart")


require_access()
with st.container(horizontal=True, vertical_alignment="center"):
    st.title("Control de cobranzas")
    st.badge("Información privada", icon=":material/shield_lock:", color="blue")
st.caption(
    "Cargá el CONRENPF una sola vez para analizar la cartera de cheques y controlar todos los movimientos con sus recibos."
)

with st.sidebar:
    st.header("Panel de análisis")
    st.caption(":material/contrast: Tema claro u oscuro desde el menú ⋮.")
    with st.expander("Archivo y fecha de análisis", icon=":material/upload_file:", expanded=True):
        uploaded = st.file_uploader(
            "Archivo CONRENPF",
            type=["xlsx", "xlsm"],
            max_upload_size=15,
            help="Excel original del CONRENPF. Máximo 15 MB.",
            key="source_file",
        )
        cutoff = st.date_input(
            "Fecha de análisis",
            value=date.today(),
            format="DD/MM/YYYY",
            help="La aplicación muestra la posición de la cartera a esta fecha y toma su mes como período principal de cobro.",
        )
        st.caption("Un cheque se considera acreditado cuando su fecha de acreditación es igual o anterior a la fecha de análisis.")
        if uploaded is not None and st.button("Descartar archivo", icon=":material/delete:", width="stretch"):
            st.cache_data.clear(); st.session_state.pop("source_file", None); st.rerun()

if uploaded is None:
    with st.container(border=True):
        st.subheader("Cargar el CONRENPF")
        st.write("Seleccioná el Excel desde el panel lateral para habilitar ambos módulos.")
        st.info(
            "La cartera incluye CH24, CH48, CPD, ECHEQ y ECHEQDIF; el control general integra esos cheques con los demás medios. El archivo se procesa en memoria y no se modifica.",
            icon=":material/info:",
        )
    st.stop()

file_bytes = uploaded.getvalue()
file_digest = hashlib.sha256(file_bytes).hexdigest()
file_fingerprint = file_digest[:12]
LOGGER.info("Procesando archivo id=%s bytes=%d", file_fingerprint, len(file_bytes))
try:
    portfolio, movements, raw = process_file(file_bytes, cutoff, PROCESSING_RULE_VERSION)
except ConcentradorError as error:
    st.error(str(error), icon=":material/error:"); st.stop()
except Exception:
    LOGGER.exception("Fallo inesperado al procesar archivo id=%s", file_fingerprint)
    st.error("No pude procesar el archivo. Confirmá que sea un CONRENPF válido y no protegido.", icon=":material/error:"); st.stop()
finally:
    del file_bytes

module = st.segmented_control(
    "Módulo",
    ["Cartera de cheques", "Control de movimientos y recibos"],
    default="Cartera de cheques",
    key="main_module",
)
control_cheques = int(movements["Tipo de registro"].eq("Cheque").sum()) if not movements.empty else 0
control_bank_movements = int(movements["Tipo de registro"].eq("Movimiento bancario").sum()) if not movements.empty else 0
st.caption(
    f"El mismo CONRENPF alimenta ambos módulos: **{control_cheques:,} cheques** y "
    f"**{control_bank_movements:,} movimientos bancarios** detectados.".replace(",", ".")
)

if module == "Control de movimientos y recibos":
    st.header("Control de movimientos y recibos")
    st.caption(
        "Integra la cartera de cheques con efectivo, transferencias, depósitos y otros movimientos bancarios. "
        "Un registro queda ‘A revisar’ cuando las fuentes informan recibos incompatibles."
    )
    if movements.empty:
        st.info(
            "Este CONRENPF no contiene registros utilizables para el control.",
            icon=":material/info:",
        )
        st.stop()

    available_dates = pd.to_datetime(movements["Fecha"], dayfirst=True, errors="coerce").dropna()
    min_date = available_dates.min().date() if not available_dates.empty else cutoff
    max_date = available_dates.max().date() if not available_dates.empty else cutoff
    with st.sidebar:
        with st.expander("Filtros del conciliador", icon=":material/filter_alt:", expanded=True):
            selected_period = st.date_input(
                "Período",
                value=(min_date, max_date),
                min_value=min_date,
                max_value=max_date,
                format="DD/MM/YYYY",
                key="movement_period",
            )
            movement_record_types = st.pills(
                "Tipo de registro",
                list(CONTROL_RECORD_TYPES),
                default=list(CONTROL_RECORD_TYPES),
                selection_mode="multi",
                key="movement_record_types",
            )
            movement_clients = st.multiselect(
                "Clientes",
                sorted(value for value in movements["Cliente"].dropna().unique() if value),
                placeholder="Todos",
                key="movement_clients",
            )
            movement_methods = st.multiselect(
                "Medios de pago",
                sorted(value for value in movements["Grupo medio de pago"].dropna().unique() if value),
                placeholder="Todos",
                key="movement_methods",
            )
            movement_states = st.pills(
                "Estado del recibo",
                list(MOVEMENT_LINK_STATES),
                default=list(MOVEMENT_LINK_STATES),
                selection_mode="multi",
                key="movement_states",
            )
            movement_search = st.text_input(
                "Buscar cliente, CUIT, recibo u operación",
                placeholder="Número o texto",
                key="movement_search",
            )
        with st.expander("Cómo se vinculan los recibos", icon=":material/help:", expanded=False):
            st.markdown(
                "**Efectivo:** prioriza el número de recibo del movimiento.  \n"
                "**Transferencias:** prioriza el comprobante de relación y la observación.  \n"
                "**Depósitos:** prioriza el comprobante de relación y luego las referencias informadas.  \n"
                "**A revisar:** dos o más fuentes muestran números diferentes."
            )

    movement_filtered = movements.copy()
    if isinstance(selected_period, (tuple, list)) and len(selected_period) == 2:
        start_date, end_date = map(pd.Timestamp, selected_period)
        movement_dates = pd.to_datetime(movement_filtered["Fecha"], dayfirst=True, errors="coerce")
        movement_filtered = movement_filtered[movement_dates.between(start_date, end_date, inclusive="both")]
    if movement_record_types:
        movement_filtered = movement_filtered[movement_filtered["Tipo de registro"].isin(movement_record_types)]
    else:
        movement_filtered = movement_filtered.iloc[0:0]
    if movement_clients:
        movement_filtered = movement_filtered[movement_filtered["Cliente"].isin(movement_clients)]
    if movement_methods:
        movement_filtered = movement_filtered[movement_filtered["Grupo medio de pago"].isin(movement_methods)]
    if movement_states:
        movement_filtered = movement_filtered[movement_filtered["Estado vínculo"].isin(movement_states)]
    else:
        movement_filtered = movement_filtered.iloc[0:0]
    if movement_search.strip():
        needle = movement_search.strip().casefold()
        searchable_columns = [
            "Cliente", "CUIT cliente", "Recibo relacionado", "N° operación", "N° cheque / eCheq", "Observación"
        ]
        searchable = (
            movement_filtered[searchable_columns]
            .fillna("")
            .astype(str)
            .agg(" ".join, axis=1)
            .str.casefold()
        )
        movement_filtered = movement_filtered[searchable.str.contains(needle, regex=False)]

    movement_summary = movement_link_summary(movement_filtered).set_index("Estado vínculo")
    type_summary = movement_type_summary(movement_filtered).set_index("Tipo de registro")
    total_movement_amount = float(pd.to_numeric(movement_filtered["Importe"], errors="coerce").fillna(0).sum())
    cheque_count = int(type_summary.at["Cheque", "Cantidad"]) if "Cheque" in type_summary.index else 0
    cheque_amount = float(type_summary.at["Cheque", "Importe"]) if "Cheque" in type_summary.index else 0.0
    bank_count = int(type_summary.at["Movimiento bancario", "Cantidad"]) if "Movimiento bancario" in type_summary.index else 0
    bank_amount = float(type_summary.at["Movimiento bancario", "Importe"]) if "Movimiento bancario" in type_summary.index else 0.0
    gross_gap = bank_amount - cheque_amount
    with st.container(horizontal=True):
        st.metric(
            f"Total controlado · {len(movement_filtered):,} reg.".replace(",", "."),
            format_currency(total_movement_amount),
            border=True,
        )
        st.metric(f"Cheques · {cheque_count:,}".replace(",", "."), format_currency(cheque_amount), border=True)
        st.metric(
            f"Movimientos bancarios · {bank_count:,}".replace(",", "."),
            format_currency(bank_amount),
            border=True,
        )
        st.metric("Brecha bruta", format_currency(gross_gap), border=True)
    st.caption(
        "La brecha bruta es movimientos bancarios menos cheques dentro del período filtrado. "
        "Sirve como señal de control y no confirma por sí sola una conciliación uno a uno."
    )
    with st.container(horizontal=True):
        for state in MOVEMENT_LINK_STATES:
            state_count = int(movement_summary.at[state, "Cantidad"]) if state in movement_summary.index else 0
            state_amount = float(movement_summary.at[state, "Importe"]) if state in movement_summary.index else 0.0
            st.metric(
                f"{state} · {state_count:,} reg.".replace(",", "."),
                format_currency(state_amount),
                border=True,
            )

    left, right = st.columns(2)
    with left, st.container(border=True, height="stretch"):
        movement_type_chart(movement_filtered)
    with right, st.container(border=True, height="stretch"):
        movement_link_chart(movement_filtered)
    with st.container(border=True):
        movement_method_chart(movement_filtered)

    st.subheader("Cruce de cheques y movimientos por recibo")
    st.caption(
        "Agrupa los registros que tienen un recibo confiable. Un mismo recibo puede reunir más de un cheque o movimiento; "
        "por eso los importes se comparan después de sumarlos."
    )
    reconciliation = receipt_reconciliation(movement_filtered)
    non_comparable = int(movement_filtered["Estado vínculo"].ne("Con recibo").sum())
    with st.container(horizontal=True):
        for result_state in RECONCILIATION_STATES:
            result_count = int(reconciliation["Resultado"].eq(result_state).sum()) if not reconciliation.empty else 0
            receipt_word = "recibo" if result_count == 1 else "recibos"
            st.metric(result_state, f"{result_count:,} {receipt_word}".replace(",", "."), border=True)
        record_word = "registro" if non_comparable == 1 else "registros"
        st.metric("Sin recibo confiable", f"{non_comparable:,} {record_word}".replace(",", "."), border=True)
    if reconciliation.empty:
        st.info(
            "No hay recibos confiables en la selección actual para cruzar ambos grupos.",
            icon=":material/link_off:",
        )
    else:
        st.dataframe(
            reconciliation,
            hide_index=True,
            height=min(430, 40 + 35 * len(reconciliation)),
            key="receipt_reconciliation_table",
            column_config={
                "Recibo": st.column_config.TextColumn(pinned=True),
                "Cheques": st.column_config.NumberColumn(format="%d"),
                "Importe cheques": st.column_config.NumberColumn(format="$ %.2f"),
                "Movimientos bancarios": st.column_config.NumberColumn(format="%d"),
                "Importe movimientos bancarios": st.column_config.NumberColumn(format="$ %.2f"),
                "Diferencia": st.column_config.NumberColumn(format="$ %.2f"),
                "Resultado": st.column_config.TextColumn(pinned=True),
            },
        )

    st.subheader("Detalle de movimientos")
    st.caption(
        f"Se muestran {len(movement_filtered):,} de {len(movements):,} registros entre cheques y movimientos bancarios. "
        "La fila de origen permite volver al registro exacto del CONRENPF.".replace(",", ".")
    )
    movement_columns = [
        "Tipo de registro",
        "Fecha",
        "Fecha ingreso / pago",
        "Fecha acreditación",
        "Fecha vencimiento",
        "Cliente",
        "CUIT cliente",
        "Importe",
        "Medio de pago",
        "Grupo medio de pago",
        "Banco / cuenta",
        "Subcuenta",
        "Estado operativo",
        "N° cheque / eCheq",
        "Estado vínculo",
        "Recibo relacionado",
        "Fuente del vínculo",
        "Fuentes detectadas",
        "Observación",
        "Fila fuente",
    ]
    st.dataframe(
        movement_filtered[movement_columns].sort_values(["Fecha", "Importe"], ascending=[False, False]),
        hide_index=True,
        height=590,
        key="movement_detail_table",
        column_config={
            "Tipo de registro": st.column_config.TextColumn(pinned=True),
            "Fecha": st.column_config.DateColumn(format="DD/MM/YYYY", pinned=True),
            "Fecha ingreso / pago": st.column_config.DateColumn(format="DD/MM/YYYY"),
            "Fecha acreditación": st.column_config.DateColumn(format="DD/MM/YYYY"),
            "Fecha vencimiento": st.column_config.DateColumn(format="DD/MM/YYYY"),
            "Cliente": st.column_config.TextColumn(pinned=True),
            "Importe": st.column_config.NumberColumn(format="$ %.2f"),
            "Estado vínculo": st.column_config.TextColumn("Estado del recibo", pinned=True),
        },
    )

    st.subheader("Exportar control de movimientos")
    full_column, filtered_column = st.columns(2)
    with full_column, st.container(border=True, height="stretch"):
        st.markdown("### :material/table_view: Resultado completo")
        st.write("Incluye todos los cheques, movimientos bancarios y sus filas fuente.")
        complete_movement_excel = make_movements_excel(movements, raw, cutoff)
        st.download_button(
            "Descargar Excel completo",
            data=complete_movement_excel,
            file_name=f"control_movimientos_completo_{cutoff:%Y-%m-%d}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            icon=":material/download:",
            type="primary",
            width="stretch",
        )
    with filtered_column, st.container(border=True, height="stretch"):
        st.markdown("### :material/filter_alt: Resultado filtrado")
        st.write("Respeta el período, tipo de registro, cliente, medio de pago, estado y búsqueda seleccionados.")
        filtered_movement_excel = make_movements_excel(movement_filtered, raw, cutoff)
        st.download_button(
            "Descargar Excel filtrado",
            data=filtered_movement_excel,
            file_name=f"control_movimientos_filtrado_{cutoff:%Y-%m-%d}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            icon=":material/download:",
            width="stretch",
        )
    st.stop()

if portfolio.empty:
    st.warning("El archivo no contiene instrumentos compatibles."); st.stop()

with st.sidebar:
    with st.expander("Movimientos para detalle y Excel", icon=":material/account_balance_wallet:", expanded=True):
        scope = st.selectbox(
            "Qué movimientos mostrar",
            [
                "Pendientes del mes",
                "Todos los pendientes",
                "Pendientes de acreditación",
                "Todos los movimientos",
                "Acreditados (AC)",
                "Rescatados (RE)",
                "Rechazados (RC)",
            ],
            key="portfolio_scope",
        )
        st.caption("Afecta el detalle en pantalla y el Excel filtrado. La portada siempre resume los cobros pendientes.")
    with st.expander("Comprobantes", icon=":material/receipt_long:", expanded=False):
        receipt_scope = st.selectbox(
            "Recibo asociado",
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
    with st.expander("Guía de estados", icon=":material/help:", expanded=False):
        st.markdown(
            "**AC · Acreditado** — el dinero ya fue acreditado.  \n"
            "**PS · Pendiente de acreditación** — el cheque todavía no se acreditó.  \n"
            "**RE · Rescatado** — el cheque fue retirado o recuperado; no es un rechazo.  \n"
            "**RC · Rechazado** — el banco rechazó el cheque."
        )

base_filtered = portfolio.copy()
if receipt_scope == "Con comprobante asociado": base_filtered = base_filtered[base_filtered["Estado recibo"].eq("Tomado")]
elif receipt_scope == "Sin comprobante asociado": base_filtered = base_filtered[base_filtered["Estado recibo"].eq("Sin recibo asociado")]
base_filtered = base_filtered[base_filtered["Tipo"].isin(selected_types)] if selected_types else base_filtered.iloc[0:0]
if selected_clients: base_filtered = base_filtered[base_filtered["Cliente"].isin(selected_clients)]
if selected_banks:
    base_filtered = base_filtered[base_filtered["Banco cheque"].map(bank_filter_group).isin(selected_banks)]
if search.strip():
    needle = search.strip().casefold()
    searchable = base_filtered[["Cliente", "CUIT cliente", "N° cheque / eCheq", "Recibo relacionado"]].fillna("").astype(str).agg(" ".join, axis=1).str.casefold()
    base_filtered = base_filtered[searchable.str.contains(needle, regex=False)]

filtered = apply_operational_scope(base_filtered, scope, cutoff)
pending_all = pending_collection(base_filtered)
pending_month = pending_for_month(base_filtered, cutoff)
expected_dates = pd.to_datetime(pending_all["Fecha prevista de cobro"], dayfirst=True, errors="coerce")
cutoff_ts = pd.Timestamp(cutoff)
next_7 = pending_all[expected_dates.between(cutoff_ts, cutoff_ts + pd.Timedelta(days=7), inclusive="both")]
overdue = pending_all[expected_dates < cutoff_ts]
without_collection_date = pending_all[expected_dates.isna()]
pending_amount = float(pending_all["Importe"].sum())
month_amount = float(pending_month["Importe"].sum())
month_label = f"{MONTH_NAMES[cutoff.month - 1]} {cutoff.year}"

with st.container(horizontal=True):
    st.metric(f"A cobrar en {month_label} · {len(pending_month):,} chq.".replace(",", "."), format_currency(month_amount), border=True)
    st.metric(f"Próximos 7 días · {len(next_7):,} chq.".replace(",", "."), format_currency(next_7["Importe"].sum()), border=True)
    st.metric(f"Vencidos sin cobrar · {len(overdue):,} chq.".replace(",", "."), format_currency(overdue["Importe"].sum()), border=True)
    st.metric(f"Pendiente total · {len(pending_all):,} chq.".replace(",", "."), format_currency(pending_amount), border=True)
st.caption(
    f":material/event_available: Posición calculada al **{cutoff:%d/%m/%Y}**. "
    "Los indicadores respetan comprobantes, tipos, clientes, bancos y búsqueda."
)

with st.container(border=True):
    st.markdown("**Lectura rápida para gerencia**")
    if pending_all.empty:
        st.write("No quedan cheques pendientes de cobro con los filtros actuales.")
    else:
        month_share = month_amount / pending_amount if pending_amount else 0.0
        client_amounts = pending_month.groupby("Cliente")["Importe"].sum().sort_values(ascending=False)
        top_three_share = float(client_amounts.head(3).sum()) / month_amount if month_amount else 0.0
        message = (
            f"El mes de **{month_label}** concentra **{month_share:.1%}** del saldo pendiente. "
            f"Los tres clientes con mayor importe representan **{top_three_share:.1%}** de lo previsto para el mes."
        )
        if not overdue.empty:
            message += f" Además, hay **{format_currency(overdue['Importe'].sum())}** con fecha de cobro ya cumplida."
        if not without_collection_date.empty:
            message += f" Hay **{len(without_collection_date):,} cheques** sin fecha prevista y requieren revisión.".replace(",", ".")
        st.write(message)

view = st.segmented_control(
    "Sección",
    ["Cobros del mes", "Detalle de cheques", "Rescatados y rechazados", "Control de recibos", "Reportes"],
    default="Cobros del mes",
)
if view == "Cobros del mes":
    with st.container(border=True):
        monthly_collection_chart(base_filtered, cutoff)
    left, right = st.columns([1.2, 1])
    with left, st.container(border=True, height="stretch"):
        client_exposure_chart(pending_month)
    with right, st.container(border=True, height="stretch"):
        donut_chart(
            pending_month,
            "Estado calculado",
            "Por qué siguen pendientes",
            "chart_pending_state",
            description="Separa pendientes normales, pendientes de acreditación y fechas vencidas.",
        )
    st.subheader(f"Cheques que deberían cobrarse en {month_label}")
    st.caption("Listado ordenado por fecha prevista de cobro. Sirve para decidir qué cheques conviene conservar o negociar.")
    if pending_month.empty:
        st.info("No hay cheques pendientes previstos para este mes.", icon=":material/event_available:")
    else:
        monthly_detail = pending_month.sort_values(["Fecha prevista de cobro", "Importe"], ascending=[True, False])
        st.dataframe(
            monthly_detail[["Fecha prevista de cobro", "Cliente", "Tipo", "N° cheque / eCheq", "Banco cheque", "Importe", "Estado calculado", "Estado recibo"]],
            hide_index=True,
            column_config={
                "Fecha prevista de cobro": st.column_config.DateColumn("Cobro previsto", format="DD/MM/YYYY", pinned=True),
                "Cliente": st.column_config.TextColumn(pinned=True),
                "Importe": st.column_config.NumberColumn(format="$ %.2f"),
                "Estado calculado": st.column_config.TextColumn("Situación"),
                "Estado recibo": st.column_config.TextColumn("Recibo"),
            },
            height=420,
            key="monthly_collection_table",
        )
    if not without_collection_date.empty:
        st.warning(
            f"Hay {len(without_collection_date):,} cheques pendientes sin fecha prevista de cobro; no aparecen en el calendario mensual.".replace(",", "."),
            icon=":material/warning:",
        )
elif view == "Detalle de cheques":
    st.subheader("Detalle de movimientos")
    st.caption(
        f"Vista seleccionada: **{scope}** · mostrando {len(filtered):,} de {len(base_filtered):,} movimientos después de aplicar los filtros.".replace(",", ".")
    )
    columns = ["Estado calculado", "Fecha prevista de cobro", "Días al cobro", "Cliente", "Importe", "Tipo", "N° cheque / eCheq", "Banco cheque", "Fecha acreditación", "Fecha vencimiento", "Código estado", "Fuente clasificación", "Estado recibo", "Recibo relacionado", "Fuente del vínculo", "Nro Cpb Relación", "Observaciones", "Código rechazo", "Motivo rechazo", "Alertas", "Fila fuente"]
    st.dataframe(
        filtered[columns],
        hide_index=True,
        column_config={
            "Estado calculado": st.column_config.TextColumn("Situación", pinned=True),
            "Fecha prevista de cobro": st.column_config.DateColumn("Cobro previsto", format="DD/MM/YYYY", pinned=True),
            "Días al cobro": st.column_config.NumberColumn(format="%d"),
            "Cliente": st.column_config.TextColumn(pinned=True),
            "Importe": st.column_config.NumberColumn(format="$ %.2f"),
            "Fecha acreditación": st.column_config.DateColumn(format="DD/MM/YYYY"),
            "Fecha vencimiento": st.column_config.DateColumn(format="DD/MM/YYYY"),
        },
        height=650,
        key="portfolio_detail_table",
    )
elif view == "Rescatados y rechazados":
    st.info(
        "RE significa **rescatado** y nunca se suma a los rechazos. RC significa **rechazado**; cuando falta un estado explícito, la app solo clasifica como rechazo si coinciden código y motivo.",
        icon=":material/info:",
    )
    with st.container(border=True):
        rescued_client_chart(base_filtered)
    with st.container(border=True):
        rejected_bank_chart(base_filtered)
    with st.container(border=True):
        rejected_trend_chart(base_filtered)
elif view == "Control de recibos":
    linked = base_filtered[base_filtered["Recibo relacionado"].ne("")]
    missing = base_filtered[base_filtered["Recibo relacionado"].eq("")]
    with st.container(horizontal=True):
        st.metric("Con recibo identificado", len(linked), border=True)
        st.metric("Sin recibo identificado", len(missing), border=True)
        st.metric("Detectados en observaciones", len(linked[linked["Fuente del vínculo"].eq("Observación")]), border=True)
        st.metric("Detectados por relación", len(linked[linked["Fuente del vínculo"].eq("Nro Cpb Relación")]), border=True)
    with st.container(border=True):
        receipt_coverage_chart(base_filtered)
    left, right = st.columns([1, 1.25])
    with left, st.container(border=True, height="stretch"):
        donut_chart(
            linked,
            "Fuente del vínculo",
            "Dónde se encontró el recibo",
            "chart_receipt_source",
            max_slices=4,
            description="Indica si el número surgió de la observación o del comprobante relacionado.",
        )
    with right, st.container(border=True, height="stretch"):
        st.subheader("Cheques sin recibo identificado")
        st.caption("Estos movimientos necesitan revisión porque no se encontró un número de recibo o comprobante asociado.")
        if missing.empty:
            st.success("Todos los cheques tienen un recibo identificado.", icon=":material/check_circle:")
        else:
            st.dataframe(
                missing[["Cliente", "Tipo", "N° cheque / eCheq", "Nro Cpb Relación", "Observaciones", "Fila fuente"]],
                hide_index=True,
                height=430,
            )
else:
    st.subheader("Descargar reportes")
    st.caption("Generá documentos para compartir la posición completa o trabajar con la vista filtrada.")
    pdf_column, excel_column = st.columns(2)
    with pdf_column, st.container(border=True, height="stretch"):
        st.markdown("### :material/picture_as_pdf: Posición completa en PDF")
        st.write("Resumen ejecutivo de cobros, composición por estado y detalle de todos los cheques.")
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
        st.markdown("### :material/table_view: Movimientos filtrados en Excel")
        st.write("Planilla operativa con la vista seleccionada y las filas fuente relacionadas.")
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
