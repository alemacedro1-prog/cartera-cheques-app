# Control de cobranzas

Aplicación privada en Streamlit que carga un único CONRENPF y ofrece dos módulos: `Cartera de cheques` y `Control de movimientos y recibos`.

## Consolidación y auditoría

- La clave de un cheque es **N° de cheque/eCheq + importe a centavos**. El número conserva sus ceros iniciales; se normalizan espacios externos, mayúsculas y el sufijo numérico `.0` de Excel.
- Sin número o sin importe válido, la fila se mantiene individual: nunca se agrupa solo por importe.
- Si existe un registro AC dentro de esa clave, el cheque se cuenta una sola vez como **Acreditado**, aun si la fecha de análisis es anterior. RC/PS quedan únicamente como historial. Entre varios AC se utiliza el de fecha de acreditación/ingreso más reciente.
- Sin AC, se usa el registro más reciente por fecha de ingreso/pago y, a igualdad o falta de fecha, por fila original. El vencimiento no se interpreta como fecha de cambio de estado.
- Cada instrumento incluye cantidad de filas, duplicados consolidados, historial de estados y todas las filas originales del CONRENPF. Los datos fuente nunca se eliminan por consolidar.
- El cuadro **Cheques rechazados que luego se acreditaron** muestra cliente, número, importe, estados anterior/final, fecha del AC, motivo histórico y filas originales. No suma esos importes a los rechazos efectivos.
- Un movimiento **MANU** sin AC, RC o PS se marca **Acreditado** y con origen **MANU - acreditado fuera del concentrador**. Si tiene AC/RC/PS, respeta ese estado. No se inventan recibos ni fechas.
- **Movimientos pendientes** separa cliente, método, fecha, importe, número de cheque, recibo, estado y origen; excluye AC y MANU acreditados.

El Excel completo incorpora `Cartera consolidada`, `Movimientos pendientes`, `Rechazados efectivos`, `Rechazados luego acreditados`, `MANU acreditados` y `Datos fuente filtrados`, además del resumen y cruce de recibos. Las descargas filtradas conservan todas las filas históricas de los registros seleccionados y no incluyen registros ajenos. La pestaña `Cartera` del Excel filtrado se mantiene por compatibilidad.

Los totales indicados para CONRENPF(4) —1.716 únicos, 42 duplicados, 2 RC→AC, 29 rechazados, 475 pendientes y 166 MANU— son referencias aportadas por el usuario. Falta verificarlos con ese archivo, que no estaba disponible durante esta implementación; no se usan como cifras fijas en la aplicación.

En la cartera de cheques, el recibo relacionado se obtiene exclusivamente de `Observación` y `Nro Cpb Relación`. Un número con formato de recibo dentro de la observación, por ejemplo `819-591309`, tiene prioridad y queda marcado como `Tomado`. También se reconoce como recibo interno de cobranza cualquier secuencia numérica de tres o más dígitos que comience con `75`, sin límite máximo de longitud; por ejemplo `756699` o `756701`. Si no aparece ninguno, se usa el comprobante de relación. `MCR-Número de recibo` no participa en el vínculo de cheques. El archivo original nunca se modifica.

El módulo de movimientos integra CH24, CH48, CPD, ECHEQ y ECHEQDIF con efectivo, transferencias, depósitos y cualquier otro medio informado. Cada fila queda identificada como `Cheque` o `Movimiento bancario`, permitiendo filtrar, comparar importes y revisar los vínculos con recibos en un único control. La brecha entre ambos grupos es orientativa y no se presenta como una conciliación automática uno a uno.

Los cheques conservan la misma regla de recibos que la cartera. Para los demás medios se comparan `Observación`, `Nro Cpb Relación`, comprobantes relacionados tipificados y `MCR-Número de recibo`. La prioridad cambia según el medio; si dos fuentes aportan números incompatibles, el movimiento queda `A revisar` y conserva todas las señales para auditoría.

El cruce por recibo suma los cheques y movimientos bancarios que comparten el mismo número normalizado. Informa si coinciden recibo e importe, si el importe es diferente o si el recibo aparece únicamente en uno de los dos grupos. Las filas sin recibo o con fuentes incompatibles se mantienen fuera del cruce y se contabilizan como pendientes de revisión.

El filtro `Comprobante asociado` permite ver todos los movimientos, solamente los tomados o solamente los que siguen sin recibo asociado. Se aplica al resumen, el detalle, la calidad de vínculos y la exportación filtrada.

La portada está orientada a la decisión gerencial: muestra el importe pendiente de cobro del mes, los próximos siete días, los vencidos sin cobrar y el saldo pendiente total. La lectura rápida informa qué proporción del saldo corresponde al mes y cuánto concentran los tres principales clientes.

El calendario de cobros usa `Fecha acreditación` y, cuando no está informada, `Fecha vencimiento`. El gráfico diario distingue fechas futuras de fechas ya cumplidas. La exposición por cliente usa un ranking horizontal de los diez mayores saldos pendientes.

El panel lateral separa la carga, la vista del detalle, los comprobantes y los filtros operativos. Las secciones principales son `Cobros del mes`, `Detalle de cheques`, `Rescatados y rechazados`, `Control de recibos` y `Reportes`.

La app trabaja únicamente con el archivo activo de la sesión. Al descartarlo, cerrar la sesión o reiniciarse el servidor, la información deja de estar disponible. Los reportes PDF y Excel se generan bajo demanda para descarga y no se conservan en la aplicación.

Para el estado de la cartera, `RE` significa `Rescatado`, `RC` significa `Rechazado`, `PS` se muestra como `Pendiente de acreditación` y `AC` como `Acreditado`. Un RE nunca se suma a los importes rechazados, aunque conserve código y motivo históricos. Cuando falta un estado explícito, la app solo usa código y motivo de rechazo si ambos están informados y no contradicen un estado operativo.

El resumen de rechazados destaca Macro, Galicia y Nación, pero conserva una categoría `Otros bancos` para que los KPIs, gráficos, PDF y Excel siempre reconcilien con el total general.

Los códigos se normalizan aunque lleguen con minúsculas, espacios, puntos, barras o una descripción adjunta. La vista `Pendientes del mes` incluye únicamente cheques que todavía representan un cobro y cuya fecha prevista pertenece al mes de la fecha de análisis.

## Seguridad y privacidad

- El Excel original se procesa en memoria y nunca se escribe en disco. Los PDF y Excel derivados se entregan como descargas y tampoco se conservan.
- El caché es por sesión, tiene vencimiento de 15 minutos y un botón para descartarlo.
- La carga está limitada a XLSX/XLSM de 15 MB, 100.000 filas y 150 MB descomprimidos.
- En internet, `require_auth` debe ser `true`. El acceso usa OIDC y una lista exacta de entre uno y cinco correos; la app rechaza configuraciones con más usuarios.
- Para una demostración local también puede usarse `auth_mode = "password"`: admite entre uno y cinco usuarios, guarda únicamente salt y hash PBKDF2-SHA256 en Secrets y mantiene la contraseña fuera del código y del paquete.
- Los secretos nunca deben guardarse en Git. Los Excel están excluidos por `.gitignore`.
- La descarga contiene solo la vista filtrada y sus filas fuente; neutraliza fórmulas de celdas aportadas por el archivo.

## Ejecución local

```powershell
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m streamlit run streamlit_app.py
```

Sin `.streamlit/secrets.toml`, la app muestra una advertencia y funciona solo como entorno local. No exponga ese modo a internet.

## Resumen inicial del archivo

Al cargar el archivo se muestra primero un resumen de cheques únicos: total, pendientes de cobro (todos los meses) y acreditados; debajo, sin recibo y con recibo. Cada tarjeta incluye cantidad e importe. El resumen usa la cartera consolidada completa, no los filtros de pantalla ni los movimientos bancarios MANU/transferencias. Los recibos se cuentan en todos los estados, incluidos los acreditados.

Debajo aparece el listado **Cheques no tomados · sin recibo asociado**, ordenado por cliente: número de cheque/eCheq, importe, fecha de acreditación, estado y filas fuente. Incluye AC sin recibo, sin limitarse al mes ni al estado pendiente. La fecha es la informada en el archivo; si falta, no se inventa ni se usa el vencimiento. El listado también está disponible en Control de recibos para la selección actual.

## Rechazos por banco de depósito

- El gráfico y el filtro bancario de cartera usan `Banco depósito` (`Nombre del banco` o `MCR-Banco depósito` en el CONRENPF), no el banco emisor del cheque.
- La comparación principal muestra Macro, Galicia y Nación. Un cheque emitido por ICBC, Credicoop o Santa Fe y depositado en Macro se cuenta en Macro.
- Los depósitos en otros bancos y los registros sin dato se conservan en el total, las estadísticas y un detalle de revisión separado; nunca se asignan usando el banco emisor como sustituto.
- El PDF usa la misma agrupación. Los Excel conservan tanto banco de depósito como banco emisor y la trazabilidad original.

## Pruebas

```powershell
.venv\Scripts\python.exe -m pytest -q
```

Las pruebas usan exclusivamente datos sintéticos y cubren la integración de cheques y movimientos bancarios, reglas de recibo por medio de pago, fuentes incompatibles, extracción ECHEQ, variantes RC/RE, bancos fuera del foco, vencimientos, validación de carga, exportación y autorización.

## Despliegue recomendado: Streamlit Community Cloud

1. Cree un repositorio **privado** y suba esta carpeta. Confirme antes que ningún Excel ni `secrets.toml` esté versionado.
2. En Streamlit Community Cloud, cree la app desde `streamlit_app.py` y pegue en **Secrets** el contenido local de `.streamlit/secrets.toml`; ese archivo nunca debe subirse al repositorio.
3. Verifique ingreso autorizado, rechazo de un usuario ajeno, carga sintética y cierre de sesión antes de usar datos reales.

El único paso externo pendiente es conectar el repositorio privado con la cuenta de Streamlit Community Cloud y cargar los usuarios autorizados en **Secrets**.

## Alternativa con contenedor

El `Dockerfile` permite desplegar en Render, Cloud Run o un servicio equivalente. Monte los secretos en `.streamlit/secrets.toml` en tiempo de ejecución, use HTTPS administrado y no agregue almacenamiento persistente. El endpoint de salud es `/_stcore/health`.

## Operación y mantenimiento

- Actualice dependencias en una rama, ejecute las pruebas y valide manualmente con un archivo sintético antes de desplegar.
- Revise trimestralmente la lista de usuarios autorizados y elimine accesos que ya no correspondan.
- Los logs registran solo un identificador SHA-256 abreviado y el tamaño; no nombres de archivo, importes, clientes ni contenido.
- Si cambia el formato CONRENPF, agregue primero un caso sintético de regresión y luego adapte `utils/portfolio.py`.
