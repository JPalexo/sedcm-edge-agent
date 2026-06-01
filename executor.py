import os
import json
import time
import uuid
import threading
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

# ── Configuración desde variables de entorno ──────────────────────────────────
BROKER_IP    = os.getenv("MQTT_HOST", "127.0.0.1")
BROKER_PORT  = int(os.getenv("MQTT_PORT", "1883"))
ZONA_ID      = os.getenv("EDGE_ZONE", "A")
RACK_ID      = os.getenv("EDGE_RACK", "A1")
EXECUTOR_ID  = os.getenv("EXECUTOR_ID", f"executor-{os.getenv('EDGE_ZONE','A')}-{os.getenv('EDGE_RACK','A1')}")
ACK_DELAY_S  = float(os.getenv("ACK_DELAY_S", "0.5"))

TOPIC_CONTROL  = f"dc/control/zona/{ZONA_ID}/rack/{RACK_ID}"
TOPIC_ACK      = f"dc/ack/zona/{ZONA_ID}/rack/{RACK_ID}"
TOPIC_ACTUATOR = f"dc/actuator/zona/{ZONA_ID}/rack/{RACK_ID}"

# TTLs por tipo de efecto (ms) — alineados con el executor Node.js
TTL_SOFT_REBOOT   = 30_000
TTL_HARD_SHUTDOWN = 45_000
TTL_HVAC          = 45_000

ALLOWED_ACTIONS = {"soft_reboot", "hard_shutdown", "set_hvac_mode"}

# ── Docker API (opcional) ─────────────────────────────────────────────────────
try:
    import docker as docker_sdk
    _docker_client = docker_sdk.from_env()
    print("[SISTEMA] API de Docker conectada correctamente.")
except Exception as _e:
    _docker_client = None
    print(f"[WARN] Docker no disponible — hard_shutdown será simulado: {_e}")


# ── Helpers ───────────────────────────────────────────────────────────────────
def _ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _derive_actuator_effect(action, mode=None):
    """Devuelve (effect_name, ttl_ms, hvac_mode) según la acción recibida."""
    if action == "soft_reboot":
        return "cpu_cooldown", TTL_SOFT_REBOOT, None
    if action == "hard_shutdown":
        return "node_shutdown", TTL_HARD_SHUTDOWN, None
    if action == "set_hvac_mode":
        hvac_mode = mode if mode in {"cooling", "humidify", "dehumidify"} else "cooling"
        return "environment_cooling", TTL_HVAC, hvac_mode
    return None, None, None


# ── Ejecución de comandos ─────────────────────────────────────────────────────
def _ejecutar_comando(client, payload):
    command_id = payload.get("command_id", "ID_DESCONOCIDO")
    action     = payload.get("action", "")
    target     = payload.get("target") or {}
    target_id  = target.get("target_id", "RACK_GENERAL")
    mode       = payload.get("mode")

    if not command_id or action not in ALLOWED_ACTIONS:
        print(f"[WARN] Comando inválido — command_id='{command_id}' action='{action}'")
        return

    print(f"\n{'='*50}")
    print(f"[ALERTA] COMANDO RECIBIDO DEL BACKEND")
    print(f"  ID Orden : {command_id}")
    print(f"  Objetivo : {target_id}")
    print(f"  Acción   : {action.upper()}")
    print(f"{'='*50}")

    ack_status = "ACKED"

    if action == "soft_reboot":
        print(f"> Simulando reinicio de software en {target_id}…")
        time.sleep(2)
        print("> Reinicio lógico completado.")

    elif action == "hard_shutdown":
        print(f"> [PELIGRO] Buscando contenedor '{target_id}' para apagarlo…")
        if _docker_client:
            try:
                contenedor = _docker_client.containers.get(target_id)
                contenedor.stop()
                print(f"> Éxito: contenedor '{target_id}' apagado.")
            except Exception as exc:
                # docker.errors.NotFound no se importa directamente para evitar dependencia dura
                err_type = type(exc).__name__
                if "NotFound" in err_type:
                    print(f"[ERROR] Contenedor '{target_id}' no encontrado en este host.")
                    ack_status = "FAILED"
                else:
                    print(f"[ERROR] Fallo al apagar contenedor: {exc}")
                    ack_status = "FAILED"
        else:
            print("> Docker no disponible — simulando apagado lógico.")
            time.sleep(2)

    elif action == "set_hvac_mode":
        hvac_target = mode or "cooling"
        print(f"> Ajustando HVAC del rack {RACK_ID} → modo '{hvac_target}'…")
        time.sleep(1)
        print("> HVAC actualizado (lógico).")

    # Publicar efecto de actuador para que collector.py lo procese
    effect, ttl_ms, hvac_mode = _derive_actuator_effect(action, mode)
    if effect:
        actuator_payload = {
            "command_id": command_id,
            "action":     action,
            "effect":     effect,
            "ttl_ms":     ttl_ms,
            "timestamp":  _ts(),
        }
        if hvac_mode:
            actuator_payload["mode"] = hvac_mode
        if target:
            actuator_payload["target"] = target

        client.publish(TOPIC_ACTUATOR, json.dumps(actuator_payload), qos=0)
        print(f"[ACTUADOR] Efecto '{effect}' publicado en {TOPIC_ACTUATOR} (TTL {ttl_ms // 1000}s)")

    # Espera configurable antes del ACK
    time.sleep(ACK_DELAY_S)

    ack_payload = {
        "timestamp_ack": _ts(),
        "command_id":    command_id,
        "metadata":      {"dc_zone": ZONA_ID, "dc_rack": RACK_ID},
        "action_executed": action,
        "status":        ack_status,
        "executor_id":   EXECUTOR_ID,
    }
    client.publish(TOPIC_ACK, json.dumps(ack_payload), qos=1)
    print(f"[ACK] '{action}' → {ack_status} reportado al backend.")


# ── Callbacks MQTT ────────────────────────────────────────────────────────────
def on_connect(client, userdata, flags, reason_code, properties):
    if not reason_code.is_failure:
        print(f"[MQTT] Executor conectado a {BROKER_IP}:{BROKER_PORT}")
        client.subscribe(TOPIC_CONTROL, qos=1)
        print(f"[MQTT] Escuchando órdenes en: {TOPIC_CONTROL}")
    else:
        print(f"[MQTT] Error de conexión: {reason_code}")


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    print(f"[MQTT] Executor desconectado ({reason_code}). Reconectando…")


def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
        # Ejecutar en hilo aparte para no bloquear el loop MQTT
        t = threading.Thread(target=_ejecutar_comando, args=(client, payload), daemon=True)
        t.start()
    except (json.JSONDecodeError, Exception) as e:
        print(f"[WARN] Payload de control inválido: {e}")


# ── Loop principal ────────────────────────────────────────────────────────────
def run_executor():
    print("─── SEDCM EDGE EXECUTOR (Python) ───")
    print(f"  Zona: {ZONA_ID}  Rack: {RACK_ID}  Executor: {EXECUTOR_ID}")
    print(f"  Broker: {BROKER_IP}:{BROKER_PORT}")

    client_id = f"{EXECUTOR_ID}-{uuid.uuid4().hex[:6]}"
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
    )
    client.on_connect    = on_connect
    client.on_disconnect = on_disconnect
    client.on_message    = on_message

    try:
        client.connect(BROKER_IP, BROKER_PORT, keepalive=60)
        client.loop_forever()
    except KeyboardInterrupt:
        print("\n[INFO] Deteniendo executor…")
    except Exception as e:
        print(f"[ERROR] No se pudo conectar al broker: {e}")
    finally:
        client.disconnect()


if __name__ == "__main__":
    run_executor()
