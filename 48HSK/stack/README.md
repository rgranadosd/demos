# Stack en contenedores — 48HSK (APIM + IS + charlas)

Reproduce **desde cero** todo el montaje de la charla en contenedores **Podman**:

1. **`wso2-apim`** — WSO2 API Manager con la **config y datos actuales horneados**
   (Key Manager IS-7, APIs OpenAI/Mistral/Shopify/WeatherMCP, scopes por operación,
   app GUARDARAILES y suscripciones). Offset 0 → `9443`/`8243`/`8280`.
2. **`wso2-is`** — WSO2 Identity Server con su config/datos (app OIDC ShopifyAgentApp
   con JWT+scopes, rol `demo-shopify`, usuario `rafa`, API resources SCIM). Offset 10 → `9453`.
3. **`charlas-48hsk`** — agente 48HSK + **Weather MCP** (servicio en `:28080`) + demo AI Gateway.

> Los datos van **horneados en las imágenes** (H2 embebida). Recrear desde la nada =
> `podman load` de las imágenes + `./up.sh`. El estado es efímero: al recrear los
> contenedores vuelves al estado conocido-bueno.

## Modelo de red (clave)

Los 3 contenedores comparten **el mismo namespace de red** (un *pod* de Podman, o
`network_mode: service:apim` en compose). Así `localhost:9443/8243/9453/8000/28080`
resuelve entre ellos igual que en el Mac, y **no hay que reescribir ninguna URL** de
la config. Funciona porque APIM (offset 0) e IS (offset 10) no colisionan de puertos.

## Requisitos

- Podman con una máquina propia (aislada del k3s):
  ```sh
  podman machine init wso2-stack --cpus 4 --memory 8192 --disk-size 60
  CONTAINERS_MACHINE_PROVIDER=applehv podman machine start wso2-stack
  ```
- Para **construir**, tener a mano los servidores ya configurados en:
  `/Users/rafaelgd/Develop/wso2/demos/apim` y `/Users/rafaelgd/Develop/wso2/demos/IS/wso2is-7.3.0`
  (ajustable con `APIM_SRC` / `IS_SRC`).

## Construir las imágenes

> Para un snapshot **limpio** de H2, **para APIM e IS** antes (evita copiar una BD en uso).
> Además, los contenedores usan los mismos puertos que los servidores nativos, así que
> **no pueden correr a la vez**: para los nativos antes de `up.sh`.

```sh
./build.sh            # apim + is + charlas
./build.sh apim       # una sola
```

## Levantar / parar

```sh
./up.sh               # crea el pod y arranca los 3 contenedores
./down.sh             # para y elimina el pod (las imágenes se conservan)
# alternativa: podman compose up -d   /   podman compose down
```

Accesos:
- IS Console: `https://localhost:9453/carbon` (admin/admin)
- APIM Publisher/DevPortal: `https://localhost:9443/publisher` · gateway `https://localhost:8243`
- Weather MCP: `http://localhost:28080/mcp`

## Ejecutar el agente

```sh
podman exec -it charlas bash -lc 'cd /opt/charlas/48HSK/AGENT && python agent_gpt4.py'
```
El login de usuario imprime una URL (`https://localhost:9453/oauth2/authorize?...`):
ábrela en el navegador del host (`rafa` / `Patata01!`). El callback vuelve a
`http://localhost:8000/callback` (publicado). Trazas `[IS]`/`[APIM]` como siempre;
`--no-debug` para ocultarlas.

## Recrear en otra máquina (desde la nada)

```sh
# en la máquina origen
podman save -o wso2-48hsk-images.tar wso2-apim:48hsk wso2-is:48hsk charlas-48hsk:48hsk
# en la máquina destino (con podman)
podman load -i wso2-48hsk-images.tar
./up.sh
```

## Notas

- Imágenes grandes (WSO2 + datos ≈ 1–2 GB cada una). Normal.
- Datos efímeros por diseño; si quieres persistencia entre recreaciones, monta
  `repository/database` como volumen (no incluido por defecto).
- No usa Docker/Colima: todo con `podman` en la máquina `wso2-stack`, aislada del k3s.
