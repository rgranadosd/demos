# WSO2 API Manager — imagen con la config y datos ACTUALES horneados.
# El "payload/" es un snapshot del servidor APIM ya configurado (H2 incluida:
# Key Manager IS-7, APIs, scopes, apps, suscripciones...). Ver build.sh.
FROM docker.io/library/eclipse-temurin:21-jre

# Utilidades que algunos scripts de WSO2 esperan (hostname, ps, netstat)
RUN apt-get update \
 && apt-get install -y --no-install-recommends hostname procps net-tools \
 && rm -rf /var/lib/apt/lists/*

ENV WSO2_HOME=/opt/wso2/apim
RUN groupadd -r wso2 && useradd -r -g wso2 -d ${WSO2_HOME} -s /usr/sbin/nologin wso2 \
 && mkdir -p ${WSO2_HOME}

# Snapshot del servidor ya configurado (offset 0)
COPY --chown=wso2:wso2 payload/ ${WSO2_HOME}/
RUN chmod +x ${WSO2_HOME}/bin/*.sh || true

# tmp/ y repository/logs/ se excluyen del snapshot: WSO2 los necesita (ConfigParser,
# java.io.tmpdir, logging). Además el DIRECTORIO RAÍZ debe ser de wso2 para que el
# server pueda crear backup/ y wso2carbon.pid al arrancar (corre como usuario wso2).
RUN mkdir -p ${WSO2_HOME}/tmp ${WSO2_HOME}/repository/logs \
 && chown wso2:wso2 ${WSO2_HOME} ${WSO2_HOME}/tmp ${WSO2_HOME}/repository/logs

USER wso2
WORKDIR ${WSO2_HOME}

# Offset 0 → gateway HTTPS 8243, HTTP 8280, mgmt/token 9443
EXPOSE 9443 8243 8280
# Java del contenedor (temurin) en vez del JDK del Mac
ENV JAVA_HOME=/opt/java/openjdk

ENTRYPOINT ["./bin/api-manager.sh"]
