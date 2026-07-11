# Rafa's Agent — Agente Shopify IA gobernado por WSO2 (IS + API Manager)

## 1. Descripción

**Rafa's Agent** es un agente conversacional de IA (Semantic Kernel + OpenAI) que
opera una tienda **Shopify** real, consulta **el tiempo** (vía un servidor **MCP**)
y razona con un **LLM** — pero con una diferencia clave respecto a un agente típico:

> **Todo el acceso del agente al mundo exterior pasa por WSO2, que actúa como
> plano de control (control plane) de la IA: autenticación, autorización y
> gobierno de las llamadas al LLM, a las herramientas (MCP) y a las APIs de negocio.**

El proyecto es una **demo de gobierno de IA / Zero Trust para agentes**. Enseña que:

- El agente **no** habla directamente con OpenAI, Shopify ni el MCP: habla con el
  **gateway de WSO2 API Manager**, que aplica seguridad y cuotas.
- La **identidad del usuario** (quién está usando el agente) la gestiona **WSO2
  Identity Server** con OAuth2/OIDC (Authorization Code + PKCE).
- La **autorización fina** ("¿puede este usuario editar precios?") la **impone el
  gateway** validando *scopes* OAuth2 por operación — **no** un `if` en el código
  del agente. Un usuario sin el scope recibe un **403 del gateway**.

Durante la ejecución, el agente **traza en vivo** cada interacción para que se vea
el flujo en tiempo real:

- `[IS]`   (magenta) → WSO2 Identity Server: login, tokens de usuario, permisos (SCIM).
- `[APIM]` (azul)    → WSO2 API Manager / Gateway: token de app, LLM, Shopify, MCP, con el código HTTP (200 / 403).

Las trazas se ocultan con `--no-debug` para una presentación limpia.

---

## 2. Diseño arquitectónico

### 2.1 Componentes

```
                                   ┌──────────────────────────────────────────┐
                                   │                Rafa's Agent               │
        Navegador                  │      (Semantic Kernel + OpenAI SDK)       │
        (login usuario)            │                                           │
            │                      │  plugins:  shopify.py   weather.py        │
            │ OIDC + PKCE          │  trazas:   trace_log.py ([IS]/[APIM])     │
            ▼                      └───────┬───────────────┬───────────────────┘
   ┌───────────────────┐    (A) client_credentials        │ (B) JWT de usuario
   │  WSO2 Identity     │◀─── login usuario (OIDC/PKCE) ───┘        (Shopify)
   │  Server  :9453     │                  │                        │
   │                    │                  ▼                        ▼
   │ • OAuth2/OIDC      │        ┌────────────────────────────────────────────┐
   │ • JWT + scopes     │        │        WSO2 API Manager (gateway)           │
   │ • Roles (RBAC)     │        │   token :9443    ·    gateway :8243         │
   │ • SCIM             │◀───────┤                                             │
   └───────────────────┘  Key    │  Key Manager: IS registrado (tipo WSO2-IS-7)│
        ▲                 Manager │  APIs:  OpenAIAPI · MistralAIAPI            │
        │ SCIM (permisos │        │         ShopifyAdminAPI (scopes/operación) │
        │ para UX)       │        │         WeatherMCP                          │
        └────────────────┼────────┤  ► impone scopes OAuth2 por operación      │
                         │        └───────┬───────────────┬───────────────┬────┘
                         │                ▼               ▼               ▼
                         │          api.openai.com   tienda Shopify   Weather MCP
                         │            (LLM)          (Admin API)      :28080 → Open-Meteo
                         │
                   GUARDARAILES (app APIM) con 2 mapeos de claves:
                   • Resident KM  → token de app (LLM/MCP)
                   • WSO2-IS-7 KM → JWT de usuario de IS (Shopify)
```

### 2.2 Dos flujos de autenticación/autorización

**(A) Token de aplicación — LLM y Weather MCP**

1. El agente pide un token **client_credentials** a APIM (`:9443`) con las
   credenciales de la app **GUARDARAILES**.
2. Con ese token de app llama al **gateway** (`:8243`): `/openaiapi/...` (LLM) y
   `/weather-mcp/...` (MCP).
3. El gateway valida la suscripción de la app y **reenvía** al backend (OpenAI /
   MCP local → Open-Meteo). No hay contexto de usuario: son recursos no sensibles
   a la identidad.

**(B) JWT de usuario — Shopify (autorización fina Zero Trust)**

1. Cuando una acción necesita Shopify, el agente lanza **login en IS** (`:9453`,
   Authorization Code + PKCE). El usuario entra (p. ej. `rafa`).
2. IS emite un **JWT de usuario** cuyos *scopes* dependen del **rol** del usuario
   (política **RBAC**: el rol `demo-shopify` concede `view_products`,
   `update_prices`, `update_descriptions`).
3. El agente llama al gateway de Shopify (`/shopify/...`) con **ese JWT de usuario**.
4. APIM reconoce el token porque **IS está registrado como Key Manager**
   (conector **WSO2-IS-7**) y el `client_id` (claim `azp`) está mapeado a la app
   suscrita GUARDARAILES. El gateway **valida el scope requerido por la operación**:
   - `GET /products…` → `view_products`
   - `PUT /products/{id}` → `update_prices`
   - `POST`/`DELETE /collects`, `POST /custom_collections` → `update_descriptions`
5. Si el token trae el scope → **200** y se reenvía a la tienda Shopify
   (con la `X-Shopify-Access-Token`). Si no → **403** (`900910 Scope validation failed`)
   **desde el gateway**.

> **SCIM** se usa además en el agente para *mostrar* los permisos del usuario
> (UX / defensa en profundidad), pero **la autoridad de autorización es el gateway**,
> no el código Python.

### 2.3 Decisiones de diseño relevantes

- **La autorización vive en el gateway, no en el cliente.** El chequeo Python
  (`_check_permission`) es un respaldo/UX que **no** cortocircuita: deja que la
  petición llegue a APIM y sea el gateway quien autorice o devuelva 403 (visible en
  el trace `[APIM]`).
- **IS como Key Manager (WSO2-IS-7).** Necesario para que el gateway acepte y valide
  los JWT emitidos por IS (el conector legacy `WSO2-IS` no es compatible con IS 7.x).
- **Límite REST conocido:** `PUT /products/{id}` cubre tanto precio como descripción
  (una sola operación REST), así que el gateway no puede separar `update_prices` de
  `update_descriptions` en ese endpoint; esa distinción fina queda en la capa Python.

### 2.4 Mapa de módulos

| Módulo | Rol |
|---|---|
| `agent_gpt4.py` → `cli.py` → `agent_core.py` | Entrypoint CLI y runtime del agente |
| `oauth2_apim.py` | Token de app (client_credentials) + cliente OpenAI contra el gateway |
| `oauth_session.py` | Login OIDC/PKCE de usuario, refresh, resolución SCIM |
| `plugins/shopify.py` | Herramientas Shopify (envían el JWT de usuario al gateway) |
| `plugins/weather.py` | Herramientas Weather (MCP por el gateway) |
| `trace_log.py` | Trazas `[IS]`/`[APIM]` |
| `ui_console.py` | UI de consola + spinner "pensando" |
| `service.py` / `main.py` / `observability.py` | Modo servicio HTTP (FastAPI + OpenTelemetry) |

---

## 3. Topología / puertos (local)

| Pieza | Puerto | Rol |
|---|---|---|
| WSO2 Identity Server 7.3 | `9453` | Login de usuario (OAuth2 AuthCode + PKCE), JWT, SCIM, roles |
| WSO2 API Manager 4.x — token | `9443` | Token de aplicación (client_credentials) |
| WSO2 API Manager 4.x — gateway | `8243` | Proxy + **imposición de scopes** (OpenAI, Shopify, Weather MCP) |
| Weather MCP (backend local) | `28080` | Servidor MCP de Open-Meteo (expuesto por APIM en `/weather-mcp/1.0.0`) |

---

## 4. Requisitos previos

1. **WSO2 IS** en `:9453` y **WSO2 APIM** en `:9443/:8243` corriendo.
2. **Python ≥ 3.10** (probado 3.12) y un **virtualenv** en `./venv`:
   ```bash
   python3 -m venv venv
   ```
   `start_demo.sh` instala/verifica las dependencias en ese venv
   (`semantic-kernel==1.37.0`, `openai<2`, `python-dotenv<2`, …).
3. **Weather MCP backend** en `:28080` (lo autoarranca `start_demo.sh`; a mano:
   `../MCP/WEATHER/run_weather_mcp.sh serve`).
4. **Fichero `.env`** (no se commitea):
   ```bash
   cp .env.example .env
   ```
   Variables clave: `WSO2_GW_URL`, `WSO2_OPENAI_API_URL`, `WSO2_APIM_TOKEN_ENDPOINT`,
   `WSO2_APIM_CONSUMER_KEY/SECRET` (app **GUARDARAILES**),
   `WSO2_TOKEN_ENDPOINT`/`WSO2_AUTH_ENDPOINT` (IS), `WSO2_CONSUMER_KEY/SECRET`
   (app OIDC **ShopifyAgentApp**), `WSO2_SCOPES`
   (incluye `view_products update_prices update_descriptions`),
   `WSO2_WEATHER_MCP_URL`, `SHOPIFY_API_TOKEN`, `SHOPIFY_STORE_URL`.

---

## 5. Ejecutar una prueba (demo)

```bash
./start_demo.sh
```

Ejecuta primero `pre_demo_check.sh` (valida variables, conectividad IS, token APIM,
OpenAI por gateway, Shopify y Weather MCP) y, si todo está OK, arranca el agente.

### Flags

| Flag | Efecto |
|---|---|
| *(ninguno)* | Trazas `[IS]`/`[APIM]` **visibles** (por defecto) |
| `--no-debug` | **Oculta** las trazas — demo limpio (el spinner "pensando" se mantiene) |
| `-d`, `--debug` | Logs detallados además de las trazas |
| `--purge` | Borra la caché de tokens y **fuerza login nuevo** (para cambiar de usuario) |
| `--force-auth` | Fuerza nueva autenticación ignorando la caché |
| `--skip-precheck` | Salta el chequeo previo |
| `--verbose` | Salida más detallada del arranque |
| `--custom <banner>` / `--list-banners` | Banner personalizado / listar banners |

> El `pre_demo_check` marca **OK** el Shopify aunque devuelva **403** con el token de app:
> ese 403 es el resultado *esperado* y demuestra que el gateway impone scopes. El acceso
> real a Shopify ocurre al iniciar sesión como usuario dentro del demo.

---

## 6. Usuarios de prueba

| Usuario | Password | Rol | Puede en Shopify |
|---|---|---|---|
| `rafa` | `Patata01!` | `demo-shopify` (View Products, Update Prices, Update Descriptions) | **Leer y editar** (200) |
| `admin` | `admin` | *(sin permisos de Shopify)* | **Nada** → gateway **403** (`900910`) |

El login se abre en el navegador la primera vez que una acción necesita el token de
usuario. Para **cambiar de usuario**, reinicia con `--purge`.

---

## 7. Guion de prueba (extremo a extremo)

Escribe estas frases en el prompt `Tú >`:

1. **`quien eres`** → responde vía LLM. Trazas: `[APIM] LLM · POST /openaiapi/...`.
2. **`dame la lista de productos`** → dispara **login en IS** (entra como `rafa`).
   `[IS] Login…` → `[IS] Token de usuario emitido` →
   `[APIM] Shopify · GET /products.json  scope: view_products` → `[200]`.
3. **`actualiza el precio de la taza wso2 y rebájala un 10%`** →
   `[APIM] Shopify · PUT /products/{id}.json  scope: update_prices` → `[200]`.
4. **`dime el tiempo en Barcelona`** → `[APIM] Weather · tool 'get_current_weather'`.
5. **`teniendo en cuenta el tiempo en Barcelona, ¿qué me recomiendas hacer en la web?`**
   → combina Weather MCP + catálogo + LLM.
6. **`ok actualiza destacados`** → gestiona "Home Page":
   `[APIM] Shopify · POST/DELETE /collects…  scope: update_descriptions`.
7. **`salir`**.

### Probar la **denegación** por el gateway (tesis Zero Trust)

```bash
./start_demo.sh --purge      # login nuevo → entra como admin/admin
# Tú > dame la lista de productos
# → [APIM] Shopify · DENEGADO por el Gateway  falta el scope 'view_products'  [403]
```
La denegación la impone **APIM**, no el código Python.

---

## 8. Ciudades disponibles para el tiempo (Weather MCP)

Madrid, Barcelona, Valencia, Sevilla, Zaragoza, Málaga, Murcia, Bilbao, Vitoria,
Alicante, Córdoba, Burgos, La Coruña, Santa Cruz de Tenerife, Buenaventura, **Alaska**.

> Los nombres se validan contra un catálogo cerrado: un typo (p. ej. *"Barceliona"*)
> devuelve `ValueError: '…' is not a valid SpanishCity`.

---

## 9. Modos de ejecución

- **CLI interactivo** (este demo): `./start_demo.sh` → `agent_gpt4.py` → `cli.py`.
- **Servicio HTTP** (`service.py`, FastAPI + OpenTelemetry): dependencias en
  `requirements-service.txt`; variables `SERVICE_*` / `OTEL_*` en `.env`.

---

## 10. Solución de problemas

- **`Error 400: redirect_uri_mismatch`** → la callback `http://localhost:8000/callback`
  debe coincidir al 100% con la registrada en la app OIDC de IS.
- **Shopify `403 / 900910`** con un usuario que debería tener permiso → confirma que su
  rol concede los scopes y que el token los lleva (`--purge` para relogin).
- **Weather MCP falla** → verifica que el backend `:28080` está arriba
  (`../MCP/WEATHER/run_weather_mcp.sh serve`).
- **Puerto de callback `8000` ocupado** → cierra instancias previas del agente.
- **Trazas molestan en pantalla** → arranca con `--no-debug`.
