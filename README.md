# Cartera de cheques

Aplicación privada en Streamlit para transformar un CONRENPF en cartera operativa, dashboard, controles de recibos y reportes profesionales en PDF y Excel.

El recibo relacionado se obtiene exclusivamente de `Observación` y `Nro Cpb Relación`. Un número con formato de recibo dentro de la observación, por ejemplo `819-591309`, tiene prioridad y queda marcado como `Tomado`. También se reconoce como recibo interno de cobranza cualquier secuencia numérica de tres o más dígitos que comience con `75`, sin límite máximo de longitud; por ejemplo `756699` o `756701`. Si no aparece ninguno, se usa el comprobante de relación. `MCR-Número de recibo` no participa en el vínculo ni en controles de cobranza, y se omite junto con `Nro Cpb Relacionado` de los Excel exportados. El archivo original nunca se modifica.

El filtro `Comprobante asociado` permite ver todos los movimientos, solamente los tomados o solamente los que siguen sin recibo asociado. Se aplica al resumen, el detalle, la calidad de vínculos y la exportación filtrada.

El resumen ejecutivo presenta los rechazados con dos lecturas directas: barras mensuales con importe y cantidad, y un ranking horizontal de clientes por importe rechazado. El detalle mensual informa cantidad, importe total y promedio, y respeta todos los filtros activos.

Los vencimientos se muestran como un flujo semanal de los próximos 90 días, con cantidad, importe y clientes en el detalle interactivo. La exposición por cliente usa un ranking horizontal de los diez principales saldos de cartera, con participación porcentual, para facilitar la lectura de concentración.

El panel lateral separa la carga, el estado de cartera, los comprobantes y los filtros operativos en secciones independientes. La planilla `Reportes` permite descargar un PDF completo —resumen ejecutivo y detalle de todos los instrumentos— y un Excel de la vista filtrada.

La app trabaja únicamente con el archivo activo de la sesión. Al descartarlo, cerrar la sesión o reiniciarse el servidor, la información deja de estar disponible. Los reportes PDF y Excel se generan bajo demanda para descarga y no se conservan en la aplicación.

Para el estado de la cartera, el código del sistema es la fuente de verdad: únicamente `RE` y `RC` se clasifican como rechazados. `PS` se muestra como `Pendiente de acreditación`, aunque la fila conserve un código o motivo de rechazo informativo.

El código `PS` se normaliza aunque llegue con minúsculas, espacios, puntos, barras o una descripción adjunta. La vista `Pend. acreditación` filtra directamente ese código y muestra la cantidad detectada en el archivo junto con la cantidad visible después de aplicar los demás filtros.

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

## Pruebas

```powershell
.venv\Scripts\python.exe -m pytest -q
```

Las pruebas usan exclusivamente datos sintéticos y cubren prioridades de recibo, extracción ECHEQ, discrepancias, estados, vencimientos, validación de carga, exportación y autorización.

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
