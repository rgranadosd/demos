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

Tres formas de enviar un justificante: arrastrarlo, pegarlo del portapapeles,
o **hacerle una foto con la cámara**. Sale la ficha del gasto: campos, líneas,
comprobaciones aritméticas y advertencias.

La imagen se voltea **siempre**, sin interruptor: la vista previa para poder
encuadrar sin confundirse, y la captura porque la cámara entrega el flujo
espejado.

Importa más de lo que parece. Con un ticket espejado el modelo **no** avisa de
que no lo lee: devuelve `legible: true` y se inventa los importes (en una
prueba, 55,50 € donde ponía 61,49). Lo único que lo delató fue la comprobación
aritmética. Es la mejor defensa del diseño: verificar en código lo que el
modelo no va a confesar.

### Captura automática

No hace falta pulsar nada: la cámara mide
cada 200 ms la nitidez y el movimiento, y dispara sola cuando la imagen lleva
unos 0,8 s enfocada y quieta. La barra de abajo muestra el nivel y el estado
(`calibrando`, `mantén la cámara quieta`, `acércate o busca el enfoque`,
`enfocado — capturando en 2…`). El botón **Capturar y analizar** sigue ahí y
pasa por delante: dispara al momento y corta el automático.

Antes de mirar el enfoque comprueba que **hay un ticket delante**, midiendo
solo dentro del marco guía. Dos señales, y las dos importan:

- **Claros**: fracción de píxeles por encima del 75% del brillo máximo del
  marco. Relativo a propósito — con un umbral de gris fijo, un ticket blanco
  bajo luz cálida daba 0% y la captura no saltaba nunca.
- **Detalle dentro de esa zona clara**: el laplaciano medido *solo* sobre los
  píxeles claros. Es lo que delata el texto impreso.

Lo segundo es la clave. Medir el detalle de todo el fotograma no vale: un techo
iluminado da 50% de claros y 3,6 de detalle, porque ese detalle viene de los
muebles oscuros de abajo. Restringido a la zona clara, la diferencia es nítida
— techo 1,07 · mesa blanca 0,00 · ticket tenue 3,71 · ticket normal 9,77. Un
factor 3,5 entre el peor ticket y el mejor falso positivo.

Esto importa porque sin ello el disparador saltaba a los dos segundos de abrir
la cámara: una habitación enfocada y quieta cumple «nítido y quieto» a la
perfección. Además el calibrado del enfoque solo corre mientras hay papel, o la
escena vacía fijaría el listón antes de que llegue el documento.

La nitidez es el laplaciano medio, que se desploma al desenfocar: sobre un
ticket real, 13,4 nítido frente a 2,9 con 1 px de desenfoque. El umbral es
**relativo** al mejor enfoque visto desde que se abrió la cámara, no un número
fijo — el valor absoluto depende de la cámara, la luz y de lo que haya delante,
así que un umbral fijo acertaría en una mesa y fallaría en otra. Hay además un
suelo absoluto para no disparar sobre una pared lisa, donde todo está «igual de
enfocado» porque no hay nada que enfocar.

La máquina de estados está probada con secuencias simuladas: no dispara sobre
una pared, ni con la mano en movimiento, ni con la imagen borrosa aunque esté
quieta; y dispara a las cuatro muestras de estabilizarse.

La cámara pide la resolución más alta disponible (1920×1080 si la hay): un
ticket tiene letra pequeña y a 640×480 el modelo no lee los importes. La
captura se envía sola, sin un clic extra, y el piloto de la cámara se apaga al
cerrar. Funciona sin HTTPS porque los navegadores tratan `127.0.0.1` como
contexto seguro.

El servidor hace de intermediario a propósito: **el navegador nunca ve la API
key**. La petición sale de él sin credencial y el servidor la reenvía al gateway
con la cabecera `X-API-Key` leída de `.env`. Así se puede proyectar la pantalla,
abrir las DevTools y no enseñar el secreto.

La cabecera de la interfaz muestra a qué URL se está llamando y si hay
credencial. En una demo eso importa: se ve que el tráfico entra por el gateway.

### Reintentos

Un envío fallido se reintenta hasta **3 veces** de forma automática, y la
burbuja va indicando `intento 2 de 3`. Si aun así falla, la tarjeta de error
trae un botón **↻ Reintentar** que reenvía *la misma foto*: en una demo no se
puede pedir que la vuelvan a hacer.

Solo se reintentan los fallos pasajeros — 408, 425, 429, 500, 502, 503, 504 y
los cortes de red. Un `401` o un `400` fallan al primer intento: no se arreglan
repitiéndolos, e insistir solo alarga el fallo delante del público.

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

## Qué queda trazado

El span de agente lleva, además de los tokens, de dónde viene la imagen y cómo
es. En el namespace `expense.document.*`, todo categórico o numérico:

    expense.document.source             api | upload | camera | cli | email
    expense.document.mime_type          image/jpeg
    expense.document.size_bytes         184320
    expense.document.image.width        1920
    expense.document.image.height       1080
    expense.document.image.orientation  landscape

El nombre del fichero **ya no se registra**: `captura-114240.jpg` es inocuo,
pero `factura-acme-marzo.jpg` no, y no hay forma de distinguirlos en tiempo de
ejecución.

Hace falta ponerlo a mano porque el SDK de Traceloop intenta subir la imagen a
`/v2/traces/{trace}/spans/{span}/images`, un endpoint de Traceloop Cloud que el
gateway de AMP **no implementa**: devolvía 404 y dejaba un span hijo en rojo en
cada llamada multimodal. Verificado — `/otel/v1/traces` responde 401 (existe),
`/otel/v2/traces/...` responde 404.

El agente sustituye ese subidor por una función propia que no llama a nadie y
devuelve un marcador corto (`_silenciar_subida_de_imagenes`). Ponerlo a `None`
no vale: el SDK entonces se salta el preprocesado y escribe la data URL entera
en el atributo del span (`chat_wrappers.py:447`) — con una foto de webcam,
cientos de KB por traza. Con el marcador: 57 caracteres en lugar de 37.848, sin
petición de red y sin 404.

Las dimensiones se leen de la cabecera del PNG o del JPEG, sin Pillow. Importan
en la traza: una captura de webcam a 1920x1080 y una miniatura a 320x240 dan
resultados muy distintos, y sin ese dato no hay forma de saber cuál se envió
cuando una extracción sale mal.

## Seguir la traza desde el cliente

El servidor genera un contexto W3C por peticion y lo manda en la cabecera
`traceparent`. El agente lo extrae y cuelga `invoke_agent ocr-agent` de ahi en
vez de abrir una traza nueva, de modo que navegador, proxy, gateway y agente
comparten un unico trace id. La ficha del resultado lo muestra al pie, listo
para copiarlo y buscarlo en Agent Manager.

Hay que extraerlo a mano: en el pod solo esta instrumentado `requests`, no hay
instrumentacion de FastAPI ni de ASGI, asi que nadie leia la cabecera entrante.

**Limite:** el span del propio cliente no se ve dibujado en AMP. Para eso
tendria que exportar sus spans a `/otel/v1/traces`, que exige la credencial de
agente (`AMP_AGENT_API_KEY`) — un cliente externo no deberia llevarla encima.
Lo que se comparte es el identificador, no los spans del cliente.

## Observabilidad

El objetivo es poder responder, **sin abrir el prompt ni la respuesta**: qué
modelo se usó, cuánto tardó cada fase, cuántos tokens costó, si el JSON y el
esquema fueron válidos, si el justificante necesita revisión humana, si hubo
reintentos y dónde falló.

El agente **no configura OpenTelemetry**. El tracer provider, el propagador y el
exporter los instala el `sitecustomize` de AMP; `observabilidad.py` solo los
toma del API global. Montar un segundo provider partiría las trazas en dos.

### Árbol de spans

```
POST /gastos/analizar                     (si hay instrumentación HTTP)
└─ invoke_agent ocr-agent                 gen_ai.operation.name = invoke_agent
   ├─ app.document.validate               mime, tamaño, firma, dimensiones
   ├─ app.document.preprocess             hoy solo el encode; sin rotar ni escalar
   ├─ chat <modelo>                       uno por intento
   ├─ app.ocr.parse_json
   ├─ app.ocr.validate_schema             contra expense-v1
   └─ app.ocr.quality_check               decide review_required
```

Y en `/gastos/registrar` con `confirmado: true`, un `app.expense.persist`.

`chat <modelo>` **solo se crea si nadie más lo está creando**. Cuando la
instrumentación automática de AMP está cargada, ya emite su propio
`openai.chat` y envolverlo en otro span de cliente sería duplicar la misma
semántica. La detección mira si el módulo está en `sys.modules`, no si
`Completions.create` tiene `__wrapped__`: el propio SDK de OpenAI decora ese
método con `functools.wraps`, así que ese atributo está siempre y el falso
positivo dejaba la llamada al modelo **sin ningún span**.

No hay `app.document.store`: el justificante no se persiste en ningún sitio. Se
deja el TODO en el código en vez de inventar una fase que no existe.

### Variables de entorno

| Variable | Por defecto | Para qué |
|---|---|---|
| `OTEL_GENAI_CAPTURE_CONTENT` | `none` | `none` \| `redacted` \| `full` |
| `OTEL_ENVIRONMENT` | `development` | Va a `deployment.environment.name` |
| `OTEL_SERVICE_VERSION` | `unknown` | Release o commit SHA |
| `OCR_AGENT_WORKFLOW_VERSION` | la de servicio | `gen_ai.agent.version` |
| `EXPENSE_OCR_SCHEMA_VERSION` | `expense-v1` | Versión del contrato validado |
| `EXPENSE_OCR_MAX_ATTEMPTS` | `2` | Intentos totales al modelo |
| `EXPENSE_OCR_MAX_BYTES` | `10485760` | Rechazo por tamaño (413) |
| `EXPENSE_OCR_ID_SALT` | — | Sin ella no se emite `expense.document.id_hash` |
| `EXPENSE_OCR_REVIEW_CONFIDENCE_THRESHOLD` | — | Reservada; hoy no hay confianza real que umbralizar |

### Captura de contenido, por entorno

| Nivel | `gen_ai.input/output.messages` | Prompt crudo del SDK | Imagen |
|---|---|---|---|
| `none` | se eliminan | se elimina | **nunca** |
| `redacted` | se conservan, con el PII enmascarado | se elimina | **nunca** |
| `full` | tal cual | tal cual | **nunca** |

- **Local:** `full` si necesitas ver el prompt.
- **Staging y producción:** `redacted`. Los evaluadores de nivel agente de AMP
  leen `gen_ai.input.messages` y `gen_ai.output.messages`; con `none` el monitor
  se ejecuta pero no tiene nada que puntuar. En `redacted` reciben la estructura,
  los importes y el `tipo_documento` — que es lo que puntúan — mientras
  `comercio`, `resumen`, `fecha` y las descripciones de línea salen como
  `[redactado]`. El enmascarado baja también por el JSON anidado dentro de
  `content`, que es donde está de verdad el comercio.

La imagen en base64 no sale **en ningún nivel**, ni siquiera en `full`: `full`
es para depurar el prompt, no para volcar el justificante en la traza.

Como un `SpanProcessor` no puede modificar un span ya cerrado, la redacción se
hace en el último punto posible: envolviendo los exporters que AMP ya registró.
Si el provider no es el del SDK y no hay nada que envolver, el log de arranque
lo dice (`no se encontro ningun exporter que envolver`) en vez de fingir que la
política está aplicada. **Si ves eso en los logs del pod, la privacidad no está
garantizada y hay que mirarlo.**

Nunca se indexan: imagen o base64, prompt completo, respuesta completa, nombre
original del fichero, comercio, resumen, líneas, fechas ni importes.

### Atributos `expense.*`

| Atributo | Valores |
|---|---|
| `expense.ocr.schema_version` | `expense-v1` |
| `expense.ocr.output_valid_json` | bool |
| `expense.ocr.output_schema_valid` | bool |
| `expense.ocr.schema_failed_rules` | códigos de regla, nunca valores del documento |
| `expense.ocr.legible` | bool |
| `expense.ocr.warning_count` | entero (el recuento, no el texto) |
| `expense.ocr.retry_count` | entero |
| `expense.ocr.review_required` | bool |
| `expense.ocr.review_reason` | `none` \| `invalid_json` \| `schema_invalid` \| `illegible` \| `missing_total` \| `model_error` \| `low_confidence` |
| `expense.document.mime_type` | enum de `MIMES_ACEPTADOS` |
| `expense.document.size_bytes` | entero |
| `expense.document.page_count` | entero |
| `expense.document.source` | `api` \| `upload` \| `camera` \| `cli` \| `email` \| `unknown` |
| `expense.document.image.width` / `.height` | entero |
| `expense.document.image.orientation` | `portrait` \| `landscape` \| `unknown` |
| `expense.document.type_hint` | `ticket` \| `factura` \| `recibo` \| `otro` \| `unknown` |
| `expense.document.id_hash` | HMAC-SHA256 truncado; solo con `EXPENSE_OCR_ID_SALT` |
| `expense.persist.backend` / `.operation` / `.result` | enums |

`expense.ocr.confidence` **no se emite**: el modelo no devuelve una confianza
calibrada y fabricarla convertiría un número inventado en una decisión de
negocio.

### Identidad del agente

AMP registra el agente como principal de primera clase en **ThunderID** y le
inyecta al pod sus credenciales OAuth2:

```
AMP_AGENTID_CLIENT_ID       el client id del agente en ThunderID
AMP_AGENTID_CLIENT_SECRET   (en el Secret del pod)
AMP_AGENTID_TOKEN_ENDPOINT  el /oauth2/token de la instancia del proyecto
AMP_AGENTID_SCOPES          vacío mientras el agente no tenga roles
```

Pero **no las conecta con las trazas**: el `sitecustomize` que instala la
instrumentación son 34 líneas que solo llaman a `Traceloop.init()` y no
mencionan esas variables. Sin esto, lo único que identificaba al agente en la
traza era `gen_ai.agent.name`, una cadena escrita en el código — si alguien la
cambia, la traza miente y nadie se entera.

[agent_identity.py](agent_identity.py) pide el token propio del agente
(`client_credentials`), saca el `sub` y publica en el span raíz:

| Atributo | Valor |
|---|---|
| `gen_ai.agent.id` | el `sub` del token, firmado por ThunderID |
| `auth.actor.type` | `agent` |
| `auth.source` | `agent_token` \| `api_key` \| `unresolved` |
| `auth.issuer` | el `iss` del token |
| `auth.delegation` | `false` — todavía no hay OBO |

El token se cachea hasta que caduca: no se llama a ThunderID en cada ticket. Y
si ThunderID no responde, **el análisis sigue** y el span dice
`auth.source = unresolved`. Que un problema de trazabilidad tumbe el servicio
sería peor que no saber quién actuó.

`auth.source = api_key` significa que no había `AMP_AGENTID_*` — el caso de una
ejecución local, donde el endpoint de ThunderID no resuelve desde fuera del
clúster.

#### El agente no puede llegar a ThunderID por la vía directa

`AMP_AGENTID_TOKEN_ENDPOINT` apunta al servicio de ThunderID en el 8090, pero el
pod corre en un **sandbox** cuya NetworkPolicy de egress solo permite:

```
DNS (53)                 -> cualquier destino
todos los puertos        -> ns openchoreo-data-plane
TCP/22893                -> ns del API platform gateway
TCP/80 y TCP/443         -> 0.0.0.0/0
```

ThunderID vive en `amp-thunder-default-default` en el **8090**, que no encaja en
ninguna. El DNS resuelve y el TCP se rechaza. AMP le da al agente credenciales
para un sitio al que su propia política le impide llegar.

Parchear la NetworkPolicy no vale: lleva un timestamp en el nombre
(`ocr-agent-2026-09-04t12-45-03z-…`) y AMP la recrea en cada despliegue.

La solución es publicar el endpoint de token **por el gateway que sí está
permitido**, con el mismo patrón que ya usa el agente para alcanzar el colector
de OpenTelemetry ([api-platform-default-default-otel-restapi]):

```bash
kubectl apply -f deploy/thunder-agentid-restapi.yaml
```

Y en el componente de AMP:

```
AGENTID_TOKEN_ENDPOINT=http://api-platform-default-default-gateway-gateway-runtime.default-default:22893/thunder/oauth2/token
```

Esa variable tiene precedencia sobre la que inyecta AMP, igual que `AMP_LLM_URL`
la tiene en `llm_binding`. Cuando está puesta, la clave de agente
(`AMP_AGENT_API_KEY`) viaja en la cabecera `x-amp-api-key` que espera el
gateway, dejando `Authorization` libre para el `client_secret_basic` que
ThunderID necesita.

### Identidad del usuario

Si la petición trae `Authorization: Bearer <token>`, el agente lo **valida
contra las claves públicas de ThunderID** y publica en el span:

| Atributo | Valor |
|---|---|
| `user.id` | el `sub` del token |
| `auth.delegation` | `true` |
| `auth.source` | `obo_token` |

Por defecto **no** se exportan el username ni el email. Si necesitas verlos
durante una depuración controlada, puedes activar explícitamente:

```
OTEL_CAPTURE_USER_PII=true
```

Con esa variable aparecerán como `user.username` y `user.email` en el span raíz.
No debe activarse en producción salvo que la política de privacidad lo permita:
son PII y tienen cardinalidad alta. El valor por defecto es `false`.

Con eso la traza responde *quién pidió el análisis*, no solo *qué agente lo
ejecutó*.

La firma se comprueba de verdad. Un `user.id` sacado de un token sin verificar
no vale nada: cualquiera podría mandar un JWT inventado y atribuirle un gasto a
otra persona. Por eso, **si llega un token y no es válido, la petición se
rechaza con 401** en vez de ignorarlo en silencio. Los `error.type` posibles:
`malformed_token`, `bad_signature`, `expired_token`, `wrong_issuer`,
`incomplete_token`, `unknown_signing_key`, `jwks_unavailable`.

Sin token, el análisis sigue como siempre: no toda petición viene de una
persona, y eso no es un error.

**Del token solo sale el `sub`.** Aunque venga `email`, `username` o `name`, no
se publican: son PII y la traza no es sitio para ellos. El `sub` de ThunderID ya
es un identificador opaco, así que no hace falta seudonimizarlo encima.

Variables:

| Variable | Para qué |
|---|---|
| `AGENTID_JWKS_URI` | De dónde bajar las claves. Por defecto se deriva de `AGENTID_TOKEN_ENDPOINT` cambiando `/oauth2/token` por `/oauth2/jwks` |
| `AGENTID_ISSUER` | Emisor esperado. Sin ella no se comprueba el `iss`, que es un agujero: un token de otro IdP con firma válida pasaría |

El JWKS se cachea una hora y se refresca solo si aparece un `kid` desconocido.

### Errores

Todo fallo no recuperable deja las tres cosas a la vez — excepción registrada,
span status `ERROR` y `error.type` — porque quien consulta las trazas filtra por
el estado estándar de OTel, no por un booleano propio. Valores de `error.type`:
`unsupported_media_type`, `file_too_large`, `invalid_image`, `corrupt_file`,
`json_parse_error`, `json_schema_validation_error`, `timeout`, `rate_limited`,
`connection_error`, `http_4xx`, `http_5xx`, `model_error`.

Los reintentos dejan un evento `gen_ai.retry` en el span de agente con
`retry.attempt`, `retry.reason`, `error.type` y `http.response.status_code`. Si
el resultado final fue correcto, **la raíz no se marca en ERROR**: el fallo se
ve en el intento, no en el conjunto.

### Métricas

Histogramas `expense.ocr.document.size` (`By`), `expense.ocr.agent.duration`
(`s`), `expense.ocr.preprocess.duration` (`s`), `expense.ocr.validation.duration`
(`s`). Contadores `expense.ocr.requests`, `.successes`, `.failures`,
`.review_required`, `.retries`. Etiquetas: `model`, `document.mime_type`,
`document.type_hint`, `environment`, `result`, y `error.type` solo en las de
error. Nunca document id, user id, comercio, importe, fecha ni request id.

### Consultas útiles

```
# Justificantes que necesitan revisión humana, por motivo
span.name = "invoke_agent ocr-agent" AND expense.ocr.review_required = true
  | stats count() by expense.ocr.review_reason

# ¿El modelo está devolviendo JSON roto?
expense.ocr.output_valid_json = false | stats count() by gen_ai.request.model

# Coste por tipo de documento
span.name = "invoke_agent ocr-agent"
  | stats sum(gen_ai.usage.total_tokens) by expense.document.type_hint

# Dónde se va el tiempo
span.name IN ("app.document.preprocess", "chat*", "app.ocr.validate_schema")
  | stats p95(duration) by span.name

# Reintentos y su causa
event.name = "gen_ai.retry" | stats count() by retry.reason
```

### Sampling

Se respeta el sampler que venga configurado; el agente no implementa el suyo.
Lo que hay que pedirle al collector, porque es donde se hace bien:

- 100% de las trazas con error.
- 100% de las trazas con `expense.ocr.review_required = true` — son las que
  alguien va a auditar a mano.
- Una muestra configurable de los éxitos.

Tail sampling dentro de la aplicación no: la app no ve la traza completa y la
plataforma sí.

### Pruebas

```bash
./.venv/bin/pip install -r requirements-dev.txt
./.venv/bin/python -m pytest tests -q
```

`requirements-dev.txt` **no va al buildpack**: en el pod, el SDK de
OpenTelemetry lo inyecta AMP. Los tests cubren documento inválido, respuesta
válida, JSON roto, esquema inválido, ilegible, reintento seguido de éxito, error
no reintentable, y que en producción no se exportan mensajes, base64 ni PII.

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
- Variables a poner en el componente de Agent Manager:
  `OTEL_GENAI_CAPTURE_CONTENT=redacted`, `OTEL_ENVIRONMENT=production`,
  `OTEL_SERVICE_VERSION=<release>`, `EXPENSE_OCR_SCHEMA_VERSION=expense-v1`.
