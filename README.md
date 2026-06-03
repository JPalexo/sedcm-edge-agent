# SEDCM — Agente Edge (Python)

Agente de borde del sistema **SEDCM** (Smart Edge Data Center Manager).
Cada PC que ejecute este agente levanta una **Zona** completa (2 racks, 4 nodos)
que aparece automáticamente en el dashboard de monitoreo del servidor central.

El agente simula la telemetría de los nodos de cómputo (CPU/RAM/red) y la
termodinámica de cada rack (temperatura/humedad), y obedece los comandos de
mitigación que envía el backend (`soft_reboot`, `hard_shutdown`, `start_node`,
`set_hvac_mode`) — todo a través del broker MQTT.

> Todo el agente corre en un único proceso unificado: **`agent.py`**.
> (`collector.py` y `executor.py` son código heredado y no se utilizan.)

---

## Requisitos

- **Docker Desktop** (Windows/Mac) o Docker Engine + Compose v2 (Linux)
- Estar en la **misma red LAN** que el servidor central de SEDCM
- La IP del servidor central (pídesela a quien lo ejecuta)

No necesitas instalar Python ni dependencias: todo corre dentro de contenedores.

---

## Puesta en marcha (invitados)

### 1. Clonar el repositorio

```bash
git clone https://github.com/JPalexo/sedcm-edge-agent.git
cd sedcm-edge-agent
```

### 2. Crear tu archivo de configuración

Copia la plantilla a `.env`:

```bash
# Windows (PowerShell o CMD)
copy .env.example .env

# Linux / Mac
cp .env.example .env
```

### 3. Editar `.env`

Abre `.env` y ajusta **al menos** estas dos variables:

| Variable | Qué poner |
|---|---|
| `MQTT_HOST` | La IP LAN del servidor central (ej. `192.168.1.75`) |
| `EDGE_ZONE` | Una letra de zona única en la LAN (`B`, `C`, `D`, …). La Zona `A` es del servidor central. |

> ⚠️ Dos compañeros **no** pueden usar la misma `EDGE_ZONE`: chocarían en el
> dashboard. Pónganse de acuerdo en quién es B, C, D…

### 4. Levantar los contenedores

```bash
docker compose up -d
```

Esto construye la imagen y arranca **4 contenedores** (un nodo cada uno):

| Contenedor | Rack | Nodo |
|---|---|---|
| `sedcm-edge-{ZONA}1-n1` | `{ZONA}1` | `{ZONA}N1` |
| `sedcm-edge-{ZONA}1-n2` | `{ZONA}1` | `{ZONA}N2` |
| `sedcm-edge-{ZONA}2-n3` | `{ZONA}2` | `{ZONA}N3` |
| `sedcm-edge-{ZONA}2-n4` | `{ZONA}2` | `{ZONA}N4` |

En segundos tu zona aparecerá en el dashboard del servidor central.

---

## Comandos útiles

```bash
docker compose ps              # Ver estado de los 4 nodos
docker compose logs -f         # Ver telemetría de todos los nodos en vivo
docker compose logs -f edge-n1 # Ver solo el nodo 1
docker compose down            # Detener y eliminar los contenedores
docker compose up -d --build   # Reconstruir tras actualizar el código (git pull)
```

---

## Cómo funciona

### Simulación del nodo (`NodeEmulator`)

CPU y RAM tienen **estado acumulativo** entre ciclos. Con ~5 % de probabilidad por
ciclo el nodo entra en **fuga de recursos** (`is_leaking`): la carga sube hasta que
el backend dispara un `soft_reboot`. Hay ~1 % de recuperación espontánea por ciclo.

### Simulación del ambiente (`EnvironmentSimulator`) e inercia térmica

La temperatura del rack sigue la carga de CPU con **inercia térmica**. Cuando un
nodo recibe `hard_shutdown`, su telemetría de nodo se silencia, pero el agente
**sigue vivo publicando la telemetría ambiental del rack**, que desciende
gradualmente simulando el enfriamiento. Cuando llega `start_node`, el nodo
"despierta" con métricas saludables y reanuda su telemetría.

### Ciclo de vida de un comando

```
Backend detecta umbral crítico (o el operador pulsa un botón en el dashboard)
  → publica comando en  dc/control/zona/{Z}/rack/{R}
  → agent.py lo recibe, verifica que el target_id sea SU nodo, y lo ejecuta
    actuando directamente sobre los objetos de simulación
  → agent.py envía el ACK al backend en  dc/ack/zona/{Z}/rack/{R}
```

---

## Variables de entorno

Todas se definen en `.env` (ver `.env.example` para la plantilla comentada):

| Variable | Por defecto | Descripción |
|---|---|---|
| `MQTT_HOST` | `127.0.0.1` | IP del broker MQTT (servidor central) |
| `MQTT_PORT` | `1883` | Puerto del broker |
| `EDGE_ZONE` | `B` | Letra de tu zona (única por PC) |
| `NODE_INTERVAL_S` | `5` | Segundos entre telemetrías de nodo |
| `ENV_INTERVAL_S` | `10` | Segundos entre telemetrías de ambiente |
| `ACK_DELAY_S` | `0.5` | Segundos de espera antes de enviar el ACK |

> `EDGE_RACK`, `NODE_ID` y `EXECUTOR_ID` los genera automáticamente
> `docker-compose.yml` a partir de `EDGE_ZONE` — no los configures a mano.

---

## Tópicos MQTT utilizados

| Dirección | Tópico |
|---|---|
| Publica telemetría de nodo | `dc/telemetria/zona/{Z}/rack/{R}/nodo/{N}` |
| Publica telemetría de ambiente | `dc/telemetria/zona/{Z}/rack/{R}/ambiente` |
| Escucha comandos del backend | `dc/control/zona/{Z}/rack/{R}` |
| Publica ACK | `dc/ack/zona/{Z}/rack/{R}` |
