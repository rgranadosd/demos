# ocr-agent — análisis de justificantes de gasto

Agente que interpreta tickets, facturas y recibos a partir de su imagen,
llamando a un modelo de visión **a través del AI Gateway de WSO2 Agent
Manager**.

```
agente  ──►  AI Gateway de AMP  ──►  el LLM que AMP decida
                                     (LM Studio local, Mistral, Azure…)
```

## Lo que demuestra

**El agente no sabe con qué modelo habla.** Cuando desde Agent Manager le
enganchas un LLM provider, AMP inyecta en el pod un par de variables
`<PREFIJO>_<N>_URL` + `<PREFIJO>_<N>_API_KEY`, y el agente las descubre en
arranque (`llm_binding.py`). Cambiar de modelo — del local a uno de la nube y
vuelta — es cambiar el provider en AMP y redesplegar. **Sin tocar una línea de
código.**

Por eso no hay ninguna URL ni ningún modelo escrito en el código. Si falta la
configuración, el agente responde `503` diciendo qué falta, en vez de tirar de
un valor por defecto que nadie eligió.

## Las reglas, y dónde vive cada una

| # | Regla | Dónde se implementa |
|---|---|---|
| 1 | Identificar tipo de documento | prompt → `tipo_documento` |
| 2 | Extraer la información | prompt → JSON estructurado |
| 3 | Resumir el gasto | prompt → `resumen` |
| 4 | Importes, impuestos, comercio, fecha, moneda, pago, líneas | prompt → campos del modelo |
| 5 | **Comprobar que los importes cuadran** | **código** (`_revisar_cuadre`) |
| 6 | Señalar datos dudosos, ilegibles o ausentes | prompt + código |
| 7 | No inventar | prompt (reglas estrictas, `null` obligatorio) |
| 8 | **No registrar sin confirmación** | **código** (`/gastos/registrar`) |
| 9 | Enseñar los datos antes de registrar | **código** (vista previa) |
| 10 | Pedir otra foto si no se lee | prompt → `legible` + advertencia |

Las reglas 5, 8 y 9 están **en código y no en el prompt** a propósito. Un prompt
es una súplica; esto tiene que ser una garantía. La aritmética no se le pide a
un modelo de lenguaje, y no debe existir ningún camino por el que el modelo
pueda dar de alta un gasto por su cuenta.

## Endpoints

| Método | Ruta | Qué hace |
|---|---|---|
| `GET` | `/health` | Estado y **qué LLM le ha asignado AMP** |
| `POST` | `/gastos/analizar` | Imagen en base64. Nunca registra |
| `POST` | `/gastos/analizar/fichero` | Subida directa (multipart) |
| `POST` | `/gastos/registrar` | Exige `confirmado: true`; si no, vista previa |

## Configuración

Todo lo inyecta AMP al desplegar. Estas variables solo se tocan en local:

| Variable | Para qué |
|---|---|
| `AMP_LLM_URL` / `AMP_LLM_API_KEY` | Fijan el binding a mano; tienen precedencia sobre el autodetectado |
| `OCR_MODEL` / `AMP_GENAI_MODEL` | Id del modelo. Sin fallback en código |
| `AMP_LLM_OPENAI_PATH` | Sufijo de path, solo si el upstream es un host pelado |
| `AMP_LLM_GATEWAY_AUTHORITY` | Fuerza una autoridad concreta |

## La demo: chat visual

```bash
./servidor.py          # abre http://127.0.0.1:8800
```

Arrastra la foto de un ticket, pégala del portapapeles o elígela, y sale la
ficha del gasto: campos, líneas, comprobaciones aritméticas y advertencias.

El servidor hace de intermediario a propósito: **el navegador nunca ve la API
key**. La petición sale de él sin credencial y el servidor la reenvía al gateway
con la cabecera `X-API-Key` leída de `.env`. Así se puede proyectar la pantalla,
abrir las DevTools y no enseñar el secreto.

La cabecera de la interfaz muestra a qué URL se está llamando y si hay
credencial. En una demo eso importa: se ve que el tráfico entra por el gateway.

### Credenciales

Crea un `.env` junto al código (está en `.gitignore`, nunca va al repo):

```
OCR_AGENT_URL=http://default-default.am-gateway.localhost:19080/ocr-agent-ocr-agent-endpoint
OCR_AGENT_API_KEY=la-clave-del-agente
```

La clave se genera en la consola de Agent Manager, en la sección de API keys del
agente. Sin ella el gateway responde `401 Valid API key required`.

## cliente.py — la versión de terminal

Cliente sin dependencias (solo librería estándar de Python 3) que llama al
agente **atravesando el gateway**, como lo haría cualquier consumidor externo.
Es la forma de demostrar que el agente es alcanzable de verdad, sin túneles.

```bash
export OCR_AGENT_URL="http://default-default.am-gateway.localhost:19080/ocr-agent-ocr-agent-endpoint"
export OCR_AGENT_API_KEY="la-clave-del-agente"   # se crea en la consola

./cliente.py ticket_ok.png       # informe formateado
./cliente.py ticket_mal.png      # el descuadre, marcado en rojo
./cliente.py foto.jpg --json     # la respuesta cruda
```

Contra el pod directamente, saltándose el gateway (útil para aislar fallos):

```bash
kubectl port-forward -n dp-default-telxius-default-279c0255 pod/<pod> 8188:8080 &
./cliente.py ticket_ok.png --url http://127.0.0.1:8188
```

Sin `--api-key` contra el gateway devuelve `401 Valid API key required`, que es
en sí mismo parte de la demostración: el modelo es local y gratuito, pero el
gateway exige credencial igualmente.

## Probarlo en local

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt

kubectl port-forward -n default-default \
  svc/api-platform-default-default-gateway-gateway-runtime 22893:22893 &

# Simula lo que AMP inyecta al enganchar un provider
AMP_LLM_URL="http://127.0.0.1:22893/lmstudio" \
AMP_LLM_API_KEY="lm-studio" \
AMP_GENAI_MODEL="qwen/qwen3-vl-8b" \
  ./.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8099

curl -X POST http://127.0.0.1:8099/gastos/analizar/fichero -F "fichero=@ticket.png"
```

## Resultados verificados

Con `qwen/qwen3-vl-8b` en LM Studio, a través del gateway:

**Ticket correcto** — extrajo comercio, fecha, total 61,49 €, base 55,90, IVA
5,59, método de pago tarjeta, categoría `restauracion` y las cuatro líneas.
`base + IVA = total` ✓, líneas cuadran ✓, sin advertencias.

**Ticket con descuadre deliberado** (total 67,20 con base 55,90 + IVA 5,59):

```
base imponible + impuestos = 61.49, pero el documento declara 67.2
```

**Y un error del propio modelo, cazado por el chequeo aritmético.** En la
primera versión del prompt, ante una línea `2  MENU DEL DIA  29,00` el modelo
tomó 29,00 como precio unitario y multiplicó por 2. Los totales seguían
cuadrando, pero las líneas sumaban 98,70 en vez de 55,90. Lo detectó
`_revisar_cuadre`, no el modelo. El prompt se corrigió para dejar claro que el
importe es el número impreso y no se calcula nunca.

Es la mejor defensa del diseño: **la verificación en código encontró un fallo
que el modelo no iba a confesar**.

## Notas de despliegue

- Build por **buildpack**: basta `requirements.txt` y el `Procfile`. Sin
  Dockerfile, a diferencia de los agentes `cpc-studio`.
- **Python 3.10+** en el build. El código usa `typing.Optional` en vez de
  `str | None` para arrancar también con el 3.9 que trae macOS.
- La instrumentación automática de AMP viene activada por defecto. Encima, el
  agente abre su propio span con `gen_ai.operation.name = invoke_agent` y los
  mensajes de entrada/salida, que es lo que el contrato exige para que AMP
  derive el `kind` y aplique los evaluadores. Con cualquier otro valor el span
  queda mudo **y no avisa**.
