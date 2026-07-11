# custom-policies

Almacén de **custom policies de WSO2 APIM** en formato comprimido (ahorra espacio) +
el instalador que las aplica al APIM nativo y reconstruye la imagen del stack.

## Contenido

- `*.tar.gz` — cada archivo es una custom policy autocontenida (código + su `build.sh`):
  - `InputSanitizationPolicy.tar.gz` — **Synapse Handler GLOBAL** que sanea el payload
    de entrada (elimina Unicode invisible y neutraliza operadores peligrosos). Se
    registra en **`deployment.toml`** (`[synapse_handlers.input_sanitization]`), así que
    aplica a **TODAS las APIs desde un solo sitio** y **persiste** al reinicio (el
    config-mapper regenera `synapse-handlers.xml` desde el TOML en cada arranque).
    Sin tocar velocity templates, sin re-desplegar API por API, sin entrypoint.
- `install.sh` — descomprime cada policy, ejecuta su `build.sh` sobre el APIM nativo
  (compila el handler como **fragment bundle de synapse-core** → `components/dropins`,
  añade el bloque a `deployment.toml` y un logger INFO) y **reconstruye la imagen `wso2-apim`**.

## Cómo funciona (el mecanismo)

WSO2 APIM soporta **Synapse Handlers globales** (`org.apache.synapse.AbstractSynapseHandler`)
declarados en `deployment.toml`:

```toml
[synapse_handlers.input_sanitization]
enabled = true
class = "com.example.apim.guardrails.InputSanitizationSynapseHandler"
```

El config-mapper lo materializa en `repository/conf/synapse-handlers.xml` en cada arranque
→ el handler corre en cada mensaje de todas las APIs. El JAR debe ser un **fragment bundle
de synapse-core** (`Fragment-Host: synapse-core`) para que el `SynapseHandlersLoader` vea la
clase (un JAR normal en `lib` da `ClassNotFoundException`). El `build.sh` de la policy ya lo
empaqueta así.

## Uso (re-aplicar tras cada actualización del APIM nativo)

```bash
cd 48HSK/custom-policies
./install.sh                 # instala en el nativo + reconstruye la imagen wso2-apim
# usar la imagen nueva:
cd ../stack && ./down.sh && ./up.sh
```

> Un cambio en `deployment.toml` requiere **reiniciar** APIM (el config-mapper corre al
> arrancar). En el stack eso es simplemente recrear el pod (`down.sh`/`up.sh`); en el
> APIM nativo, reinícialo. Como el root logger está en `ERROR`, el `build.sh` añade un
> logger INFO para `com.example.apim.guardrails` y así se ven las trazas del saneo.

Variables: `APIM_HOME`, `JAVA_HOME` (JDK para `javac`), `BUILD_IMAGE=0` (instalar sin reconstruir imagen).

## Editar una policy

```bash
tar xzf InputSanitizationPolicy.tar.gz
# edita InputSanitizationPolicy/...
tar czf InputSanitizationPolicy.tar.gz --exclude target --exclude '*.class' InputSanitizationPolicy
rm -rf InputSanitizationPolicy
```
