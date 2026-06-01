import os
import json
import time
import uuid
import threading
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

from emuladores import NodeEmulator, EnvironmentSimulator, build_seeded_rng

# ── Configuración desde variables de entorno ──────────────────────────────────
BROKER_IP     = os.getenv("MQTT_HOST", "127.0.0.1")
BROKER_PORT   = int(os.getenv("MQTT_PORT", "1883"))
ZONA_ID       = os.getenv("EDGE_ZONE", "A")
RACK_ID       = os.getenv("EDGE_RACK", "A1")
NODO_ID       = os.getenv("NODE_ID",   "nodo_web_01")
INTERVALO_SEG = float(os.getenv("NODE_INTERVAL_S", "5"))
ENV_INTERVALO_SEG = float(os.getenv("ENV_INTERVAL_S", "10"))

TOPIC_NODO    = f"dc/telemetria/zona/{ZONA_ID}/rack/{RACK_ID}/nodo/{NODO_ID}"
TOPIC_RACK    = f"dc/telemetria/zona/{ZONA_ID}/rack/{RACK_ID}/ambiente"
TOPIC_ACTUATOR = f"dc/actuator/zona/{ZONA_ID}/rack/{RACK_ID}"

# ── Estado de efectos activos (enviados por el executor) ─────────────────────
# Cada efecto tiene: {"expires_at": float (epoch), "mode": str|None}
_effects_lock = threading.Lock()
_effects = {
    "cpu_cooldown":        None,
    "node_shutdown":       None,
    "environment_cooling": None,
}


def _effect_active(key):
    with _effects_lock:
        e = _effects.get(key)
        if e is None:
            return False
        if time.time() >= e["expires_at"]:
            _effects[key] = None
            print(f"[EFECTO] '{key}' expirado.")
            return False
        return True


def _apply_effect(payload):
    """Procesa un mensaje del tópico dc/actuator/ y activa el efecto correspondiente."""
    effect  = payload.get("effect", "")
    ttl_ms  = payload.get("ttl_ms", 30000)
    mode    = payload.get("mode")
    expires = time.time() + ttl_ms / 1000.0

    with _effects_lock:
        if effect == "cpu_cooldown":
            _effects["cpu_cooldown"] = {"expires_at": expires, "mode": None}
            print(f"[EFECTO] cpu_cooldown activado por {ttl_ms / 1000:.0f}s")

        elif effect == "node_shutdown":
            _effects["node_shutdown"] = {"expires_at": expires, "mode": None}
            print(f"[EFECTO] node_shutdown activado por {ttl_ms / 1000:.0f}s — pausando telemetría")

        elif effect == "environment_cooling":
            hvac_mode = mode if mode in {"cooling", "humidify", "dehumidify"} else "cooling"
            _effects["environment_cooling"] = {"expires_at": expires, "mode": hvac_mode}
            rack.set_hvac_mode(hvac_mode)
            print(f"[EFECTO] environment_cooling activado — HVAC modo '{hvac_mode}' por {ttl_ms / 1000:.0f}s")


# ── Inicialización de emuladores ──────────────────────────────────────────────
rng  = build_seeded_rng()
nodo = NodeEmulator(NODO_ID, rng=rng)
rack = EnvironmentSimulator(RACK_ID, rng=rng)


# ── Callbacks MQTT ────────────────────────────────────────────────────────────
def on_connect(client, userdata, flags, reason_code, properties):
    if not reason_code.is_failure:
        print(f"[MQTT] Conectado a {BROKER_IP}:{BROKER_PORT}")
        client.subscribe(TOPIC_ACTUATOR, qos=1)
        print(f"[MQTT] Escuchando efectos en: {TOPIC_ACTUATOR}")
    else:
        print(f"[MQTT] Error de conexión: {reason_code}")


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    print(f"[MQTT] Desconectado ({reason_code}). Reconectando…")


def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
        _apply_effect(payload)
    except (json.JSONDecodeError, Exception) as e:
        print(f"[WARN] Payload de actuador inválido: {e}")


# ── Publicación de telemetría ─────────────────────────────────────────────────
def ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def publicar_nodo(client):
    """Actualiza el emulador y publica telemetría de nodo si no está en shutdown."""

    # Aplicar cooldown de CPU si está activo (resetea el nodo)
    if _effect_active("cpu_cooldown") and nodo.is_leaking:
        nodo.soft_reboot()
        print("[EFECTO] cpu_cooldown aplicado — nodo reseteado.")

    # Silenciar telemetría durante node_shutdown
    if _effect_active("node_shutdown"):
        print(f"[SILENCIO] node_shutdown activo — sin telemetría de {NODO_ID}")
        return

    nodo.update_metrics()
    payload = {
        "timestamp": ts(),
        "metadata": {"dc_zone": ZONA_ID, "dc_rack": RACK_ID, "node_id": NODO_ID},
        "metrics":   nodo.get_payload(),
    }
    client.publish(TOPIC_NODO, json.dumps(payload), qos=0)
    m = nodo.get_payload()
    leak_tag = " [FUGA]" if nodo.is_leaking else ""
    print(f"[NODO{leak_tag}] CPU:{m['cpu_usage_pct']}% RAM:{m['ram_usage_mb']}MB → {BROKER_IP}")


def publicar_ambiente(client):
    """Actualiza el entorno con la carga CPU actual y publica telemetría ambiental."""
    carga_cpu = nodo.get_payload()["cpu_usage_pct"]
    rack.update_environment(carga_cpu)

    payload = {
        "timestamp": ts(),
        "metadata":  {"dc_zone": ZONA_ID, "dc_rack": RACK_ID},
        "environment": rack.get_payload(),
    }
    client.publish(TOPIC_RACK, json.dumps(payload), qos=0)
    e = rack.get_payload()
    print(f"[RACK ] Temp:{e['temperature_c']}°C Hum:{e['humidity_pct']}% HVAC:{rack.hvac_mode.upper()} → {BROKER_IP}")


# ── Loop principal ────────────────────────────────────────────────────────────
def run_collector():
    print("─── SEDCM EDGE COLLECTOR (Python) ───")
    print(f"  Zona: {ZONA_ID}  Rack: {RACK_ID}  Nodo: {NODO_ID}")
    print(f"  Broker: {BROKER_IP}:{BROKER_PORT}")
    print(f"  Intervalo nodo: {INTERVALO_SEG}s  |  Intervalo ambiente: {ENV_INTERVALO_SEG}s")

    collector_id = f"py-collector-{ZONA_ID}-{RACK_ID}-{uuid.uuid4().hex[:6]}"
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=collector_id,
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

    ciclo       = 1
    last_env_ts = 0.0

    try:
        while True:
            publicar_nodo(client)

            # Ambiente se publica con su propio intervalo
            if time.time() - last_env_ts >= ENV_INTERVALO_SEG:
                publicar_ambiente(client)
                last_env_ts = time.time()

            ciclo += 1
            time.sleep(INTERVALO_SEG)

    except KeyboardInterrupt:
        print("\n[INFO] Deteniendo collector…")
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    run_collector()
