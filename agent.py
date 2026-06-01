"""
SEDCM Edge Agent — proceso unificado por rack.

Combina la colección de telemetría y la ejecución de comandos en un solo proceso
para que el executor pueda actuar directamente sobre los objetos de emulación,
sin necesidad del tópico intermedio dc/actuator/.

Suscripciones MQTT:
  - dc/control/zona/{Z}/rack/{R}  (comandos del backend)
Publicaciones MQTT:
  - dc/telemetria/zona/{Z}/rack/{R}/nodo/{N}
  - dc/telemetria/zona/{Z}/rack/{R}/ambiente
  - dc/ack/zona/{Z}/rack/{R}
"""
import os
import json
import time
import uuid
import threading
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

from emuladores import NodeEmulator, EnvironmentSimulator, build_seeded_rng

# ── Configuración desde variables de entorno ──────────────────────────────────
BROKER_IP         = os.getenv("MQTT_HOST", "127.0.0.1")
BROKER_PORT       = int(os.getenv("MQTT_PORT", "1883"))
ZONA_ID           = os.getenv("EDGE_ZONE", "A")
RACK_ID           = os.getenv("EDGE_RACK", "A1")
NODO_ID           = os.getenv("NODE_ID", "nodo_web_01")
EXECUTOR_ID       = os.getenv("EXECUTOR_ID", f"executor-{os.getenv('EDGE_ZONE','A')}-{os.getenv('EDGE_RACK','A1')}")
INTERVALO_SEG     = float(os.getenv("NODE_INTERVAL_S", "5"))
ENV_INTERVALO_SEG = float(os.getenv("ENV_INTERVAL_S", "10"))
ACK_DELAY_S       = float(os.getenv("ACK_DELAY_S", "0.5"))

TOPIC_NODO    = f"dc/telemetria/zona/{ZONA_ID}/rack/{RACK_ID}/nodo/{NODO_ID}"
TOPIC_RACK    = f"dc/telemetria/zona/{ZONA_ID}/rack/{RACK_ID}/ambiente"
TOPIC_CONTROL = f"dc/control/zona/{ZONA_ID}/rack/{RACK_ID}"
TOPIC_ACK     = f"dc/ack/zona/{ZONA_ID}/rack/{RACK_ID}"

ALLOWED_ACTIONS = {"soft_reboot", "hard_shutdown", "set_hvac_mode", "start_node"}

# ── Emuladores compartidos entre colección y ejecución ───────────────────────
rng  = build_seeded_rng()
nodo = NodeEmulator(NODO_ID, rng=rng)
rack = EnvironmentSimulator(RACK_ID, rng=rng)

# ── Estado de ciclo de vida del nodo ─────────────────────────────────────────
_state_lock   = threading.Lock()
_nodo_apagado = False


def _is_node_shutdown():
    with _state_lock:
        return _nodo_apagado


def _set_node_shutdown(value: bool):
    global _nodo_apagado
    with _state_lock:
        _nodo_apagado = value


# ── Helpers ───────────────────────────────────────────────────────────────────
def _ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Ejecución de comandos — acceso directo a los objetos de emulación ─────────
def _ejecutar_comando(client, payload):
    command_id  = payload.get("command_id", "ID_DESCONOCIDO")
    action      = payload.get("action", "")
    target      = payload.get("target") or {}
    target_id   = target.get("target_id", NODO_ID)
    target_type = target.get("target_type", "rack")
    mode        = payload.get("mode")

    if not command_id or action not in ALLOWED_ACTIONS:
        print(f"[WARN] Comando inválido — command_id='{command_id}' action='{action}'")
        return

    # Comandos dirigidos a un nodo específico: solo el nodo correcto los ejecuta.
    # Varios agentes comparten el mismo tópico de control por rack; sin esta
    # guardia, un hard_shutdown para N1 también silenciaría N2 (fan-out bug).
    NODE_LEVEL_ACTIONS = {"soft_reboot", "hard_shutdown", "start_node"}
    if action in NODE_LEVEL_ACTIONS and target_type == "nodo" and target_id != NODO_ID:
        print(f"[IGNORADO] '{action}' para '{target_id}' — este nodo es '{NODO_ID}'")
        return

    print(f"\n{'='*50}")
    print(f"[ALERTA] COMANDO RECIBIDO DEL BACKEND")
    print(f"  ID Orden : {command_id}")
    print(f"  Objetivo : {target_id} (tipo: {target_type})")
    print(f"  Acción   : {action.upper()}")
    print(f"{'='*50}")

    ack_status = "ACKED"

    if action == "soft_reboot":
        # Resetea directamente el objeto nodo — sin vuelta por MQTT
        nodo.soft_reboot()
        print(f"> soft_reboot aplicado — CPU/RAM de {NODO_ID} reseteados a estado base.")

    elif action == "hard_shutdown":
        # Patrón BMC: apagado lógico únicamente — el proceso sigue vivo para escuchar start_node
        _set_node_shutdown(True)
        print(f"> Nodo {NODO_ID} marcado como APAGADO — telemetría silenciada, listener MQTT activo.")

    elif action == "start_node":
        # El contenedor ya está corriendo (patrón BMC) — solo despertar lógicamente
        nodo.soft_reboot()
        _set_node_shutdown(False)
        print(f"> Nodo {NODO_ID} despertado — CPU/RAM reseteados a estado base, telemetría reanudada.")

    elif action == "set_hvac_mode":
        hvac_target = mode if mode in {"cooling", "humidify", "dehumidify"} else "cooling"
        # Actúa directamente sobre el objeto rack — sin vuelta por MQTT
        rack.set_hvac_mode(hvac_target)
        print(f"> HVAC de rack {RACK_ID} ajustado → modo '{hvac_target}' (acceso directo al objeto).")

    time.sleep(ACK_DELAY_S)

    ack_payload = {
        "timestamp_ack":   _ts(),
        "command_id":      command_id,
        "metadata":        {"dc_zone": ZONA_ID, "dc_rack": RACK_ID},
        "action_executed": action,
        "status":          ack_status,
        "executor_id":     EXECUTOR_ID,
    }
    client.publish(TOPIC_ACK, json.dumps(ack_payload), qos=1)
    print(f"[ACK] '{action}' → {ack_status} reportado al backend.")


# ── Publicación de telemetría ─────────────────────────────────────────────────
def publicar_nodo(client):
    if _is_node_shutdown():
        print(f"[SILENCIO] Nodo APAGADO — sin telemetría de {NODO_ID}")
        return

    nodo.update_metrics()
    payload = {
        "timestamp": _ts(),
        "metadata":  {"dc_zone": ZONA_ID, "dc_rack": RACK_ID, "node_id": NODO_ID},
        "metrics":   nodo.get_payload(),
    }
    client.publish(TOPIC_NODO, json.dumps(payload), qos=0)
    m = nodo.get_payload()
    leak_tag = " [FUGA]" if nodo.is_leaking else ""
    print(f"[NODO{leak_tag}] CPU:{m['cpu_usage_pct']}% RAM:{m['ram_usage_mb']}MB → {BROKER_IP}")


def publicar_ambiente(client):
    # Inercia térmica: si el nodo está apagado, cpu_efectiva=0 → enfriamiento gradual
    carga_cpu = 0.0 if _is_node_shutdown() else nodo.get_payload()["cpu_usage_pct"]

    rack.update_environment(carga_cpu)
    payload = {
        "timestamp":   _ts(),
        "metadata":    {"dc_zone": ZONA_ID, "dc_rack": RACK_ID},
        "environment": rack.get_payload(),
    }
    client.publish(TOPIC_RACK, json.dumps(payload), qos=0)
    e = rack.get_payload()
    estado = "APAGADO" if _is_node_shutdown() else "activo"
    print(f"[RACK ] Temp:{e['temperature_c']}°C Hum:{e['humidity_pct']}% HVAC:{rack.hvac_mode.upper()} [nodo {estado}]")


# ── Callbacks MQTT ────────────────────────────────────────────────────────────
def on_connect(client, userdata, flags, reason_code, properties):
    if not reason_code.is_failure:
        print(f"[MQTT] Agente conectado a {BROKER_IP}:{BROKER_PORT}")
        client.subscribe(TOPIC_CONTROL, qos=1)
        print(f"[MQTT] Escuchando órdenes en: {TOPIC_CONTROL}")
    else:
        print(f"[MQTT] Error de conexión: {reason_code}")


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    print(f"[MQTT] Desconectado ({reason_code}). Reconectando…")


def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
        # Ejecutar en hilo separado para no bloquear el loop MQTT
        t = threading.Thread(target=_ejecutar_comando, args=(client, payload), daemon=True)
        t.start()
    except (json.JSONDecodeError, Exception) as e:
        print(f"[WARN] Payload de control inválido: {e}")


# ── Loop principal ────────────────────────────────────────────────────────────
def run_agent():
    print("─── SEDCM EDGE AGENT (Python) ───")
    print(f"  Zona: {ZONA_ID}  Rack: {RACK_ID}  Nodo: {NODO_ID}  Executor: {EXECUTOR_ID}")
    print(f"  Broker: {BROKER_IP}:{BROKER_PORT}")
    print(f"  Intervalo nodo: {INTERVALO_SEG}s  |  Intervalo ambiente: {ENV_INTERVALO_SEG}s")

    agent_id = f"py-agent-{ZONA_ID}-{RACK_ID}-{uuid.uuid4().hex[:6]}"
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=agent_id,
    )
    client.on_connect    = on_connect
    client.on_disconnect = on_disconnect
    client.on_message    = on_message

    try:
        client.connect(BROKER_IP, BROKER_PORT, keepalive=60)
        client.loop_start()
    except Exception as e:
        print(f"[ERROR] No se pudo conectar al broker: {e}")
        return

    last_env_ts = 0.0

    try:
        while True:
            publicar_nodo(client)

            if time.time() - last_env_ts >= ENV_INTERVALO_SEG:
                publicar_ambiente(client)
                last_env_ts = time.time()

            time.sleep(INTERVALO_SEG)

    except KeyboardInterrupt:
        print("\n[INFO] Deteniendo agente…")
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    run_agent()