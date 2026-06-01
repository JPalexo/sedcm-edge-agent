# SEDCM — Agente Edge Python

Agente de borde para el sistema **SEDCM** (Smart Edge Data Center Manager).
Simula la telemetría de un nodo de cómputo y el ambiente físico de un rack, y los
publica al backend central a través del broker MQTT.

Cada computadora que ejecute este agente aparece automáticamente como una nueva
**Zona** en el dashboard de monitoreo.

---

## Requisitos

- Python 3.10 o superior
- pip
- Acceso de red a la máquina que corre el backend SEDCM (misma red LAN)

---

## Instalación y uso

### 1. Instalar dependencias

```bash
pip install -r requirements.txt
```

### 2. Configurar la conexión

```bash
# En Windows
copy config.env.example config.env

# En Linux / Mac
cp config.env.example config.env
```

Abre `config.env` y cambia al menos estas tres variables:

| Variable | Qué cambiar |
|---|---|
| `MQTT_HOST` | IP de la máquina principal que corre el backend (ej. `192.168.1.10`) |
| `EDGE_ZONE` | Una letra única para tu zona (ej. `C`, `D`, `E`) |
| `EDGE_RACK` | ID del rack dentro de tu zona (ej. `C1`) |

> Cada compañero debe usar una zona diferente para que aparezcan como nodos distintos en el dashboard.

### 3. Ejecutar (dos terminales)

**Terminal 1 — Collector** (publica telemetría cada 5 s):

```bash
python collector.py
```

**Terminal 2 — Executor** (recibe comandos del backend):

```bash
python executor.py
```

---

## Variables de configuración

Todas las variables se definen en `config.env`:

| Variable | Valor por defecto | Descripción |
|---|---|---|
| `MQTT_HOST` | `127.0.0.1` | IP del broker MQTT (máquina principal) |
| `MQTT_PORT` | `1883` | Puerto del broker |
| `EDGE_ZONE` | `A` | Letra de tu zona (debe ser única por PC) |
| `EDGE_RACK` | `A1` | ID del rack dentro de la zona |
| `NODE_ID` | `nodo_web_01` | Nombre del nodo que simulas |
| `NODE_INTERVAL_S` | `5` | Segundos entre publicaciones de telemetría de nodo |
| `ENV_INTERVAL_S` | `10` | Segundos entre publicaciones de telemetría ambiental |
| `SIM_SEED` | *(vacío)* | Semilla del RNG para reproducibilidad (opcional) |
| `EXECUTOR_ID` | `executor-A-A1` | Identificador del executor en los ACKs |
| `ACK_DELAY_S` | `0.5` | Segundos de espera antes de enviar ACK al backend |

---

## Cómo funciona

### Simulación del nodo (`NodeEmulator`)

Las métricas de CPU y RAM tienen **estado acumulativo** entre ciclos — no son números aleatorios
independientes. Con una probabilidad del 5 % por ciclo, el nodo entra en modo **fuga de recursos**
(`is_leaking`): la carga crece progresivamente hasta alcanzar el límite o hasta que el backend
dispara un `soft_reboot`. Hay un 1 % de probabilidad de recuperación espontánea por ciclo.

### Simulación del ambiente (`EnvironmentSimulator`)

La temperatura del rack sigue la carga CPU con **inercia térmica** (converge 18 % por ciclo hacia
`target_temp = 21 °C + (cpu_pct / 100) × 10 °C`). El HVAC puede activarse en modos `cooling`,
`humidify` o `dehumidify` según los comandos del backend, y se apaga automáticamente cuando
alcanza el objetivo.

### Ciclo de vida de un comando

```
Backend detecta umbral crítico
  → publica comando en dc/control/zona/{Z}/rack/{R}
  → executor.py lo recibe y ejecuta (soft_reboot, hard_shutdown, set_hvac_mode)
  → executor.py publica efecto en dc/actuator/zona/{Z}/rack/{R}
  → collector.py recibe el efecto y lo aplica a la simulación
  → executor.py envía ACK al backend en dc/ack/zona/{Z}/rack/{R}
```

---

## Tópicos MQTT utilizados

| Dirección | Tópico |
|---|---|
| Publica telemetría nodo | `dc/telemetria/zona/{Z}/rack/{R}/nodo/{N}` |
| Publica telemetría ambiente | `dc/telemetria/zona/{Z}/rack/{R}/ambiente` |
| Escucha comandos | `dc/control/zona/{Z}/rack/{R}` |
| Publica ACK | `dc/ack/zona/{Z}/rack/{R}` |
| Escucha/publica efectos | `dc/actuator/zona/{Z}/rack/{R}` |
