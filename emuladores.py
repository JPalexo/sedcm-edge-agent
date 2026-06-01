import os
import random


class NodeEmulator:
    """Simula un nodo de cómputo con métricas de CPU, RAM y red.
    La carga evoluciona con estado entre ciclos — incluye fugas graduales de recursos."""

    def __init__(self, node_id, rng=None):
        self.node_id = node_id
        # RNG compartido permite correlación realista con el entorno del rack
        self.rng = rng or random.Random()
        self.cpu_pct = 15.0
        self.ram_mb = 512.0
        self.net_rx = 400.0   # bytes/seg
        self.net_tx = 350.0   # bytes/seg
        self.is_leaking = False
        self.leak_severity = 0.0

    def update_metrics(self):
        """Evoluciona las métricas un ciclo. Puede iniciar una fuga gradual de recursos
        (5 % de probabilidad), que aumenta la carga acumulativamente hasta recuperación
        espontánea (1 %) o intervención del backend (soft_reboot)."""

        # Inicio de falla gradual
        if not self.is_leaking and self.rng.random() < 0.05:
            self.is_leaking = True
            self.leak_severity = self.rng.uniform(0.2, 0.4)

        if self.is_leaking:
            self.leak_severity = min(1.0, self.leak_severity + self.rng.uniform(0.03, 0.08))
            self.cpu_pct += self.rng.uniform(0.8, 2.5) * (1.0 + self.leak_severity)
            self.ram_mb  += self.rng.uniform(15.0, 40.0) * (1.0 + self.leak_severity)
            # La red también se dispara cuando el nodo está en crisis
            self.net_rx  += self.rng.uniform(50.0, 200.0) * self.leak_severity
            self.net_tx  += self.rng.uniform(40.0, 180.0) * self.leak_severity

            # Recuperación espontánea poco frecuente
            if self.rng.random() < 0.01:
                self.is_leaking = False
                self.leak_severity = 0.0
        else:
            self.cpu_pct += self.rng.uniform(-2.5, 2.5)
            self.ram_mb  += self.rng.uniform(-15.0, 15.0)
            self.net_rx  += self.rng.uniform(-50.0, 50.0)
            self.net_tx  += self.rng.uniform(-40.0, 40.0)

        # Límites del hardware simulado
        self.cpu_pct = max(1.0,   min(100.0,  self.cpu_pct))
        self.ram_mb  = max(100.0, min(2048.0, self.ram_mb))
        self.net_rx  = max(0.0,   min(50000.0, self.net_rx))
        self.net_tx  = max(0.0,   min(50000.0, self.net_tx))

    def soft_reboot(self):
        """Resetea el nodo a estado saludable inicial — ejecutado por el backend."""
        self.cpu_pct = 15.0
        self.ram_mb = 512.0
        self.net_rx = 400.0
        self.net_tx = 350.0
        self.is_leaking = False
        self.leak_severity = 0.0

    def get_payload(self):
        return {
            "cpu_usage_pct":    round(self.cpu_pct, 1),
            "ram_usage_mb":     round(self.ram_mb, 1),
            "net_rx_bytes_sec": round(self.net_rx, 1),
            "net_tx_bytes_sec": round(self.net_tx, 1),
        }


class EnvironmentSimulator:
    """Simula el ambiente físico del rack con inercia térmica.
    La temperatura sigue la carga de CPU con convergencia gradual (no saltos bruscos).
    El HVAC puede activarse en distintos modos para regular temperatura y humedad."""

    VALID_MODES = {"off", "cooling", "humidify", "dehumidify"}

    def __init__(self, rack_id, rng=None):
        self.rack_id = rack_id
        self.rng = rng or random.Random()
        self.temp_c = 22.0
        self.humidity_pct = 50.0
        self.hvac_mode = "off"

    def update_environment(self, carga_cpu_rack):
        """Actualiza temperatura y humedad según la carga CPU del rack y el estado del HVAC.
        La temperatura converge gradualmente hacia su objetivo — nunca salta de golpe."""

        carga_cpu_rack = max(0.0, min(100.0, carga_cpu_rack))

        if self.hvac_mode == "cooling":
            self.temp_c      -= self.rng.uniform(0.8, 1.6)
            self.humidity_pct -= self.rng.uniform(0.3, 0.8)
            if self.temp_c <= 22.0:
                self.hvac_mode = "off"

        elif self.hvac_mode == "humidify":
            self.humidity_pct += self.rng.uniform(0.5, 1.2)
            if self.humidity_pct >= 45.0:
                self.hvac_mode = "off"

        elif self.hvac_mode == "dehumidify":
            self.humidity_pct -= self.rng.uniform(0.5, 1.2)
            if self.humidity_pct <= 55.0:
                self.hvac_mode = "off"

        else:
            # Inercia térmica: la temperatura converge hacia su objetivo según la carga CPU
            target_temp   = 21.0 + (carga_cpu_rack / 100.0) * 10.0
            thermal_drift = (target_temp - self.temp_c) * 0.18
            thermal_noise = self.rng.uniform(-0.15, 0.15)
            self.temp_c  += thermal_drift + thermal_noise

            # Pico de calor cuando la carga es extrema
            if carga_cpu_rack >= 90.0:
                self.temp_c += self.rng.uniform(0.2, 0.6)

            target_humidity  = 45.0 + (self.temp_c - 22.0) * 1.2
            humidity_drift   = (target_humidity - self.humidity_pct) * 0.10
            humidity_noise   = self.rng.uniform(-0.2, 0.2)
            self.humidity_pct += humidity_drift + humidity_noise

        # Límites físicos del sensor
        self.temp_c       = max(15.0, min(60.0,  self.temp_c))
        self.humidity_pct = max(10.0, min(90.0, self.humidity_pct))

    def set_hvac_mode(self, mode):
        normalized = mode.strip().lower()
        if normalized not in self.VALID_MODES:
            raise ValueError(f"Modo HVAC inválido: '{mode}'. Válidos: {self.VALID_MODES}")
        self.hvac_mode = normalized

    def get_payload(self):
        return {
            "temperature_c": round(self.temp_c, 1),
            "humidity_pct":  round(self.humidity_pct, 1),
        }


def build_seeded_rng():
    """Crea un RNG con semilla opcional desde SIM_SEED (reproducibilidad en pruebas)."""
    seed_value = os.getenv("SIM_SEED")
    if seed_value is None:
        return random.Random()
    try:
        return random.Random(int(seed_value))
    except ValueError:
        return random.Random(seed_value)
