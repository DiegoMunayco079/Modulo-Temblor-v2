import csv
import json
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
import tkinter as tk
from tkinter import messagebox, ttk

import cbor2  # SOPORTE PARA CBOR
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
import paho.mqtt.client as mqtt

# ============================================================
# CONFIGURACIÓN DE RUTAS Y CARPETAS
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_FOLDER = os.path.join(BASE_DIR, "Datos_csv")
JSON_FOLDER = os.path.join(BASE_DIR, "Datos_json")

os.makedirs(CSV_FOLDER, exist_ok=True)
os.makedirs(JSON_FOLDER, exist_ok=True)

# Subtipos fijos para la prueba de Temblor
SUBTIPO_TEMBLOR = [
    "401 - Reposo",
    "402 - Postural",
    "403 - Cinético",
]

# ============================================================
# CONFIGURACIÓN MQTT
# ============================================================
MQTT_BROKER = "127.0.0.1"  # Cambiado a Broker Local (Mosquitto) o IP de la laptop
MQTT_PORT = 1883
MQTT_TOPIC_SUB = "temblores/wearable_t2/#"  # Escucha /cbor y /json simultáneamente
MQTT_QOS = 0

MAX_POINTS = 3000

# ============================================================
# VARIABLES GLOBALES
# ============================================================
mqtt_client = None
mqtt_connected = False

latest_data = {
    "timestamp": 0,
    "ax": 0.0, "ay": 0.0, "az": 0.0,
    "gx": 0.0, "gy": 0.0, "gz": 0.0,
    "freq_peak": 0.0, "psd_peak": 0.0,
    "energy_4_7": 0.0, "energy_2_10": 0.0, "band_ratio": 0.0,
    "mqtt_pub_latency_us": 0, "mqtt_conn_latency_us": 0,
    "format_type": "N/A",
}

network_stats = {
    "packets_received": 0, "packets_lost": 0, "loss_rate": 0.0,
    "jitter_ms": 0.0, "latency_ms": 0.0, "effective_hz": 0.0,
    "bandwidth_kbs": 0.0, "inter_arrival_ms": 0.0, "total_bytes": 0, "last_seq": -1,
}

is_recording = False
recording_buffer = []
current_cod_id = ""
current_metadata = {}
recording_start_time = 0.0

# Valores de condiciones de prueba (editables en la interfaz)
test_conditions = {
    "distancia_router_m": "",
    "tipo_red_wifi": "",
    "notas": "",
}

last_tx_time = None
last_rx_time = None
start_time_window = None
samples_counter_window = 0
bytes_counter_window = 0

data_lock = threading.Lock()

# ============================================================
# HISTORIAL DE DATOS
# ============================================================
time_data = deque(maxlen=MAX_POINTS)
ax_data = deque(maxlen=MAX_POINTS)
ay_data = deque(maxlen=MAX_POINTS)
az_data = deque(maxlen=MAX_POINTS)

gx_data = deque(maxlen=MAX_POINTS)
gy_data = deque(maxlen=MAX_POINTS)
gz_data = deque(maxlen=MAX_POINTS)

freq_peak_data = deque(maxlen=MAX_POINTS)
psd_peak_data = deque(maxlen=MAX_POINTS)
energy_4_7_data = deque(maxlen=MAX_POINTS)
energy_2_10_data = deque(maxlen=MAX_POINTS)
band_ratio_data = deque(maxlen=MAX_POINTS)


def format_codigo_paciente(raw_val):
    clean = "".join(filter(str.isdigit, raw_val))
    if not clean:
        return "0001"
    num = int(clean)
    return f"0{num:03d}"[:4]


def toggle_recording():
    global is_recording, recording_buffer, current_cod_id, current_metadata, recording_start_time

    with data_lock:
        if not is_recording:
            cod_paciente = format_codigo_paciente(entry_codigo.get())
            entry_codigo.delete(0, tk.END)
            entry_codigo.insert(0, cod_paciente)

            prueba_str = cb_prueba.get().split(" - ")[0]
            tipo_str = cb_tipo.get().split(" - ")[0]
            subtipo_str = cb_subtipo.get().split(" - ")[0]
            lado_str = cb_lado.get().split(" - ")[0]

            utc_timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

            current_cod_id = f"P{cod_paciente}_PR{prueba_str}_TP{tipo_str}_ST{subtipo_str}_L{lado_str}_{utc_timestamp}"

            test_conditions["distancia_router_m"] = entry_distancia.get().strip()
            test_conditions["tipo_red_wifi"] = cb_red_wifi.get().strip()
            test_conditions["notas"] = entry_notas.get().strip()

            current_metadata = {
                "codigo_paciente": cod_paciente,
                "prueba": int(prueba_str),
                "tipo": int(tipo_str),
                "subtipo": int(subtipo_str),
                "lado": int(lado_str),
                "timestamp_utc": utc_timestamp,
                "condiciones_prueba": {
                    "distancia_router_m": test_conditions["distancia_router_m"],
                    "tipo_red_wifi": test_conditions["tipo_red_wifi"],
                    "notas": test_conditions["notas"],
                },
            }

            is_recording = True
            recording_buffer = []
            recording_start_time = time.time()

            set_controls_state("disabled")
            record_btn.config(
                text="⏹️ Detener y Guardar", style="Recording.TButton"
            )
            rec_status_label.config(
                text=f"🔴 GRABANDO: {cod_paciente} (00:00 s)", foreground="red"
            )
        else:
            is_recording = False
            data_to_save = list(recording_buffer)
            recording_id = current_cod_id
            meta_to_save = dict(current_metadata)

            set_controls_state("readonly")
            entry_codigo.config(state="normal")
            record_btn.config(text="🔴 Iniciar Grabación", style="Normal.TButton")
            rec_status_label.config(
                text="Guardando archivos...", foreground="orange"
            )

    if not is_recording:
        save_data_files(recording_id, meta_to_save, data_to_save)


def set_controls_state(state_mode):
    entry_codigo.config(state="normal" if state_mode == "readonly" else state_mode)
    cb_tipo.config(state=state_mode)
    cb_subtipo.config(state=state_mode)
    cb_lado.config(state=state_mode)
    cb_prueba.config(state="disabled")
    entry_distancia.config(state=state_mode)
    cb_red_wifi.config(state="normal" if state_mode == "readonly" else state_mode)
    entry_notas.config(state=state_mode)


def save_data_files(cod_id, metadata, data):
    if not data:
        rec_status_label.config(
            text="Grabación cancelada (0 muestras recibidas)", foreground="gray"
        )
        return

    csv_filename = os.path.join(CSV_FOLDER, f"{cod_id}.csv")
    json_filename = os.path.join(JSON_FOLDER, f"{cod_id}.json")

    try:
        condiciones = metadata.get("condiciones_prueba", {})
        dist = condiciones.get("distancia_router_m", "")
        red = condiciones.get("tipo_red_wifi", "")
        notas = condiciones.get("notas", "")

        with open(csv_filename, mode="w", newline="", encoding="utf-8") as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow([
                "seq", "sample_time_ms", "tx_time_ms",
                "ax_ms2", "ay_ms2", "az_ms2",
                "gx_dps", "gy_dps", "gz_dps",
                "pc_rx_time_ms", "freq_peak_hz", "psd_peak",
                "energy_4_7", "energy_2_10", "band_ratio",
                "esp32_pub_latency_us", "esp32_conn_latency_us", "format",
                "distancia_router_m", "tipo_red_wifi", "notas"
            ])
            for row in data:
                writer.writerow([
                    row["seq"], row["sample_time_ms"], row["tx_time_ms"],
                    row["ax"], row["ay"], row["az"],
                    row["gx"], row["gy"], row["gz"],
                    row["pc_rx_time_ms"], row["freq_peak"], row["psd_peak"],
                    row["energy_4_7"], row["energy_2_10"], row["band_ratio"],
                    row["mqtt_pub_latency_us"], row["mqtt_conn_latency_us"],
                    row.get("format", "N/A"),
                    dist, red, notas
                ])

        json_payload = {
            "codigo_toma": cod_id,
            "paciente_info": metadata,
            "protocolo": "MQTT QoS 0",
            "frecuencia_nominal_hz": 100,
            "unidades": {"aceleracion": "m/s^2", "giroscopio": "dps"},
            "fecha_grabacion_local": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_muestras": len(data),
            "datos": data,
        }
        with open(json_filename, mode="w", encoding="utf-8") as json_file:
            json.dump(json_payload, json_file, indent=4)

        rec_status_label.config(
            text=f"✅ Guardado: {cod_id} ({len(data)} muestras)",
            foreground="green",
        )

    except Exception as e:
        rec_status_label.config(text="Error al guardar toma", foreground="red")
        messagebox.showerror("Error de Guardado", f"No se pudo guardar la toma: {e}")


def on_connect(client, userdata, flags, rc, properties=None):
    global mqtt_connected
    if rc == 0:
        mqtt_connected = True
        status_label.config(
            text=f"● MQTT CONECTADO ({MQTT_BROKER})", foreground="green"
        )
        client.subscribe(MQTT_TOPIC_SUB, qos=MQTT_QOS)
    else:
        mqtt_connected = False
        status_label.config(
            text=f"ERROR CONEXIÓN MQTT (Código {rc})", foreground="red"
        )


def on_disconnect(client, userdata, rc, properties=None):
    global mqtt_connected
    mqtt_connected = False
    status_label.config(
        text="● MQTT DESCONECTADO (Reconectando...)", foreground="orange"
    )


def on_message(client, userdata, msg):
    global last_tx_time, last_rx_time
    global start_time_window, samples_counter_window, bytes_counter_window

    rx_time_ms = time.time() * 1000.0
    packet_bytes = len(msg.payload)
    data_dict = {}
    format_type = "UNKNOWN"

    # DECODIFICACIÓN ADAPTATIVA (CBOR / JSON)
    try:
        if msg.topic.endswith("/cbor"):
            data_dict = cbor2.loads(msg.payload)
            format_type = "CBOR"
        elif msg.topic.endswith("/json"):
            payload_str = msg.payload.decode("utf-8")
            data_dict = json.loads(payload_str)
            format_type = "JSON"
        else:
            try:
                data_dict = cbor2.loads(msg.payload)
                format_type = "CBOR"
            except Exception:
                payload_str = msg.payload.decode("utf-8")
                data_dict = json.loads(payload_str)
                format_type = "JSON"

        # EXTRACCIÓN SEGURA DE VALORES
        seq = int(data_dict.get("seq", 0))
        timestamp = int(data_dict.get("sample_time_ms", data_dict.get("timestamp", 0)))
        tx_time_ms = float(data_dict.get("tx_time_ms", rx_time_ms))

        ax, ay, az = float(data_dict.get("ax", 0.0)), float(data_dict.get("ay", 0.0)), float(data_dict.get("az", 0.0))
        gx, gy, gz = float(data_dict.get("gx", 0.0)), float(data_dict.get("gy", 0.0)), float(data_dict.get("gz", 0.0))

        freq_peak = float(data_dict.get("freq_peak", 0.0))
        psd_peak = float(data_dict.get("psd_peak", 0.0))
        energy_4_7 = float(data_dict.get("energy_4_7", 0.0))
        energy_2_10 = float(data_dict.get("energy_2_10", 0.0))
        band_ratio = float(data_dict.get("band_ratio", 0.0))
        
        mqtt_pub_latency_us = int(data_dict.get("mqtt_pub_latency_us", data_dict.get("mqtt_latency_us", 0)))
        mqtt_conn_latency_us = int(data_dict.get("mqtt_conn_latency_us", 0))

        with data_lock:
            if network_stats["last_seq"] != -1 and seq > 0:
                expected_seq = network_stats["last_seq"] + 1
                if seq > expected_seq:
                    network_stats["packets_lost"] += seq - expected_seq

            if seq > 0:
                network_stats["last_seq"] = seq

            network_stats["packets_received"] += 1
            network_stats["total_bytes"] += packet_bytes

            total_expected = network_stats["packets_received"] + network_stats["packets_lost"]
            if total_expected > 0:
                network_stats["loss_rate"] = (network_stats["packets_lost"] / total_expected) * 100.0

            real_latency = rx_time_ms - tx_time_ms
            network_stats["latency_ms"] = max(0.0, real_latency)

            if last_tx_time is not None and last_rx_time is not None:
                d_tx = tx_time_ms - last_tx_time
                d_rx = rx_time_ms - last_rx_time
                current_jitter = abs(d_rx - d_tx)
                network_stats["jitter_ms"] += (current_jitter - network_stats["jitter_ms"]) * 0.1
                network_stats["inter_arrival_ms"] += (d_rx - network_stats["inter_arrival_ms"]) * 0.1

            last_tx_time, last_rx_time = tx_time_ms, rx_time_ms

            if start_time_window is None:
                start_time_window = rx_time_ms

            samples_counter_window += 1
            bytes_counter_window += packet_bytes

            elapsed_sec = (rx_time_ms - start_time_window) / 1000.0
            if elapsed_sec >= 0.2:
                network_stats["effective_hz"] = samples_counter_window / elapsed_sec
                network_stats["bandwidth_kbs"] = (bytes_counter_window / elapsed_sec) / 1024.0
                start_time_window = rx_time_ms
                samples_counter_window = 0
                bytes_counter_window = 0

            latest_data["timestamp"] = timestamp
            latest_data["ax"], latest_data["ay"], latest_data["az"] = ax, ay, az
            latest_data["gx"], latest_data["gy"], latest_data["gz"] = gx, gy, gz
            latest_data["freq_peak"] = freq_peak
            latest_data["psd_peak"] = psd_peak
            latest_data["energy_4_7"] = energy_4_7
            latest_data["energy_2_10"] = energy_2_10
            latest_data["band_ratio"] = band_ratio
            latest_data["mqtt_pub_latency_us"] = mqtt_pub_latency_us
            latest_data["mqtt_conn_latency_us"] = mqtt_conn_latency_us
            latest_data["format_type"] = format_type

            time_data.append(timestamp / 1000.0 if timestamp > 0 else rx_time_ms / 1000.0)
            ax_data.append(ax)
            ay_data.append(ay)
            az_data.append(az)
            gx_data.append(gx)
            gy_data.append(gy)
            gz_data.append(gz)
            freq_peak_data.append(freq_peak)
            psd_peak_data.append(psd_peak)
            energy_4_7_data.append(energy_4_7)
            energy_2_10_data.append(energy_2_10)
            band_ratio_data.append(band_ratio)

            if is_recording:
                recording_buffer.append({
                    "seq": seq,
                    "sample_time_ms": timestamp,
                    "tx_time_ms": tx_time_ms,
                    "ax": round(ax, 3), "ay": round(ay, 3), "az": round(az, 3),
                    "gx": round(gx, 3), "gy": round(gy, 3), "gz": round(gz, 3),
                    "pc_rx_time_ms": round(rx_time_ms, 2),
                    "freq_peak": round(freq_peak, 3),
                    "psd_peak": psd_peak,
                    "energy_4_7": energy_4_7,
                    "energy_2_10": energy_2_10,
                    "band_ratio": round(band_ratio, 5),
                    "mqtt_pub_latency_us": mqtt_pub_latency_us,
                    "mqtt_conn_latency_us": mqtt_conn_latency_us,
                    "format": format_type,
                })

    except Exception as e:
        print("Error decodificando trama MQTT:", e)


def start_mqtt_client():
    global mqtt_client
    try:
        try:
            mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        except AttributeError:
            mqtt_client = mqtt.Client()

        mqtt_client.on_connect = on_connect
        mqtt_client.on_disconnect = on_disconnect
        mqtt_client.on_message = on_message

        status_label.config(
            text=f"CONECTANDO A BROKER ({MQTT_BROKER})...", foreground="blue"
        )
        mqtt_client.connect_async(MQTT_BROKER, MQTT_PORT, keepalive=60)
        mqtt_client.loop_start()

    except Exception as e:
        status_label.config(
            text=f"ERROR AL CONFIGURAR MQTT: {e}", foreground="red"
        )


def update_values():
    with data_lock:
        timestamp = latest_data["timestamp"]
        ax, ay, az = latest_data["ax"], latest_data["ay"], latest_data["az"]
        gx, gy, gz = latest_data["gx"], latest_data["gy"], latest_data["gz"]

        freq_peak = latest_data["freq_peak"]
        psd_peak = latest_data["psd_peak"]
        energy_4_7 = latest_data["energy_4_7"]
        energy_2_10 = latest_data["energy_2_10"]
        band_ratio = latest_data["band_ratio"]
        pub_lat_us = latest_data["mqtt_pub_latency_us"]
        conn_lat_us = latest_data["mqtt_conn_latency_us"]
        fmt = latest_data["format_type"]

        rx_count = network_stats["packets_received"]
        lost_count = network_stats["packets_lost"]
        loss_rate = network_stats["loss_rate"]
        latency = network_stats["latency_ms"]
        jitter = network_stats["jitter_ms"]
        hz = network_stats["effective_hz"]
        bw = network_stats["bandwidth_kbs"]
        inter_arrival = network_stats["inter_arrival_ms"]

        rec_active = is_recording
        rec_meta = current_metadata.get("codigo_paciente", "")
        rec_start = recording_start_time

    value_ax.config(text=f"{ax:8.2f}")
    value_ay.config(text=f"{ay:8.2f}")
    value_az.config(text=f"{az:8.2f}")

    value_gx.config(text=f"{gx:8.2f}")
    value_gy.config(text=f"{gy:8.2f}")
    value_gz.config(text=f"{gz:8.2f}")

    value_freq_peak.config(text=f"{freq_peak:5.2f} Hz")
    value_psd_peak.config(text=f"{psd_peak:.2e}")
    value_energy_4_7.config(text=f"{energy_4_7:.2e}")
    value_energy_2_10.config(text=f"{energy_2_10:.2e}")
    value_band_ratio.config(text=f"{band_ratio:6.4f}")

    value_esp32_pub.config(text=f"{pub_lat_us} µs ({pub_lat_us/1000.0:.2f} ms)")
    value_esp32_conn.config(text=f"{conn_lat_us} µs ({conn_lat_us/1000.0:.2f} ms)")

    timestamp_label.config(text=f"Timestamp ESP32: {timestamp} ms | Formato: {fmt}")
    samples_label.config(text=f"Muestras Recibidas: {rx_count}")

    metric_loss.config(text=f"{loss_rate:5.2f}% ({lost_count} perdidos)")
    metric_latency.config(text=f"{latency:5.1f} ms")
    metric_jitter.config(text=f"{jitter:5.2f} ms")
    metric_hz.config(text=f"{hz:5.1f} Hz")
    metric_bw.config(text=f"{bw:5.2f} KB/s")
    metric_interval.config(text=f"{inter_arrival:5.1f} ms")

    if rec_active:
        elapsed_sec = time.time() - rec_start
        mins = int(elapsed_sec) // 60
        secs = int(elapsed_sec) % 60
        rec_status_label.config(
            text=f"🔴 GRABANDO Paciente {rec_meta} ({mins:02d}:{secs:02d} s)",
            foreground="red",
        )

    root.after(30, update_values)


def update_plot():
    try:
        with data_lock:
            t = list(time_data)
            ax, ay, az = list(ax_data), list(ay_data), list(az_data)
            gx, gy, gz = list(gx_data), list(gy_data), list(gz_data)
            f_peak = list(freq_peak_data)
            p_peak = list(psd_peak_data)

        if len(t) > 1:
            current_time = t[-1]
            window_size = 5.0
            min_x = max(0, current_time - window_size)
            max_x = max(window_size, current_time)

            n_acc = min(len(t), len(ax), len(ay), len(az))
            if n_acc > 1:
                line_ax.set_data(t[:n_acc], ax[:n_acc])
                line_ay.set_data(t[:n_acc], ay[:n_acc])
                line_az.set_data(t[:n_acc], az[:n_acc])
                ax_plot.set_xlim(min_x, max_x)
                ax_plot.set_ylim(-25, 25)

            n_gyro = min(len(t), len(gx), len(gy), len(gz))
            if n_gyro > 1:
                line_gx.set_data(t[:n_gyro], gx[:n_gyro])
                line_gy.set_data(t[:n_gyro], gy[:n_gyro])
                line_gz.set_data(t[:n_gyro], gz[:n_gyro])
                gyro_plot.set_xlim(min_x, max_x)

                recent_g = gx[-300:] + gy[-300:] + gz[-300:]
                max_g_val = max([abs(v) for v in recent_g], default=100)
                max_g_val = max(max_g_val, 100)
                gyro_plot.set_ylim(-max_g_val * 1.25, max_g_val * 1.25)

            n_scat = min(len(f_peak), len(p_peak))
            if n_scat > 0:
                recent_f = f_peak[-100:]
                recent_p = p_peak[-100:]
                m_scat = min(len(recent_f), len(recent_p))
                if m_scat > 0:
                    line_scatter_hist.set_data(recent_f[:m_scat], recent_p[:m_scat])
                    line_scatter_curr.set_data([recent_f[-1]], [recent_p[-1]])
                    max_p = max(recent_p) if max(recent_p) > 0 else 0.5
                    scatter_plot.set_ylim(0, max(0.5, max_p * 1.3))

            n_phase = min(len(ax), len(gy))
            if n_phase > 0:
                recent_ax = ax[-300:]
                recent_gy = gy[-300:]
                m_phase = min(len(recent_ax), len(recent_gy))
                if m_phase > 0:
                    line_phase.set_data(recent_ax[:m_phase], recent_gy[:m_phase])
                    line_phase_curr.set_data([recent_ax[-1]], [recent_gy[-1]])
                    max_gy_val = max([abs(v) for v in recent_gy], default=100)
                    max_gy_val = max(max_gy_val, 100)
                    phase_plot.set_ylim(-max_gy_val * 1.25, max_gy_val * 1.25)

            canvas.draw_idle()

    except Exception as e:
        print(f"Error renderizando gráficas: {e}")
    finally:
        root.after(40, update_plot)


def close_program():
    try:
        if mqtt_client:
            mqtt_client.loop_stop()
            mqtt_client.disconnect()
    except:
        pass
    root.destroy()


# ============================================================
# VENTANA TKINTER
# ============================================================
root = tk.Tk()
root.title("Proyecto Parkinson - Adquisición & Configuración de Toma")
root.geometry("1550x980")
root.protocol("WM_DELETE_WINDOW", close_program)

style = ttk.Style()
style.configure("Normal.TButton", font=("Arial", 10, "bold"))
style.configure("Recording.TButton", font=("Arial", 10, "bold"), foreground="red")

title = ttk.Label(root, text="PROYECTO PARKINSON", font=("Arial", 20, "bold"))
title.pack(pady=(5, 0))

status_label = ttk.Label(root, text="INICIALIZANDO...", font=("Arial", 10, "bold"))
status_label.pack(pady=2)

# PANEL DE CONTROL DE CAPTURA CON OPCIONES
record_frame = ttk.LabelFrame(root, text="CONFIGURACIÓN DE LA PRUEBA / PACIENTE")
record_frame.pack(pady=5, fill="x", padx=20)

ttk.Label(record_frame, text="Código (0XXX):").grid(row=0, column=0, padx=5, pady=5, sticky="e")
entry_codigo = ttk.Entry(record_frame, width=8, font=("Arial", 10))
entry_codigo.grid(row=0, column=1, padx=5, pady=5, sticky="w")
entry_codigo.insert(0, "0001")

ttk.Label(record_frame, text="Prueba:").grid(row=0, column=2, padx=5, pady=5, sticky="e")
cb_prueba = ttk.Combobox(
    record_frame,
    values=["2 - Temblor"],
    state="disabled",
    width=16,
)
cb_prueba.grid(row=0, column=3, padx=5, pady=5, sticky="w")
cb_prueba.current(0)

ttk.Label(record_frame, text="Tipo:").grid(row=0, column=4, padx=5, pady=5, sticky="e")
cb_tipo = ttk.Combobox(
    record_frame,
    values=["0 - Control", "1 - Parkinson"],
    state="readonly",
    width=15,
)
cb_tipo.grid(row=0, column=5, padx=5, pady=5, sticky="w")
cb_tipo.current(1)

ttk.Label(record_frame, text="Subtipo:").grid(row=1, column=0, padx=5, pady=5, sticky="e")
cb_subtipo = ttk.Combobox(
    record_frame,
    values=SUBTIPO_TEMBLOR,
    state="readonly",
    width=18,
)
cb_subtipo.grid(row=1, column=1, columnspan=2, padx=5, pady=5, sticky="w")
cb_subtipo.current(0)

ttk.Label(record_frame, text="Lado:").grid(row=1, column=2, padx=5, pady=5, sticky="e")
cb_lado = ttk.Combobox(
    record_frame,
    values=["1 - Derecha", "2 - Izquierda"],
    state="readonly",
    width=16,
)
cb_lado.grid(row=1, column=3, padx=5, pady=5, sticky="w")
cb_lado.current(0)

record_btn = ttk.Button(
    record_frame, text="🔴 Iniciar Grabación", style="Normal.TButton", command=toggle_recording
)
record_btn.grid(row=1, column=4, columnspan=2, padx=15, pady=5, sticky="w")

# ============================================================
# CONDICIONES DE PRUEBA (NOTAS / RED / DISTANCIA)
# ============================================================
ttk.Label(record_frame, text="Dist. Router (m):").grid(row=2, column=0, padx=5, pady=5, sticky="e")
entry_distancia = ttk.Entry(record_frame, width=8, font=("Arial", 10))
entry_distancia.grid(row=2, column=1, padx=5, pady=5, sticky="w")
entry_distancia.insert(0, "")

ttk.Label(record_frame, text="Tipo Red WiFi:").grid(row=2, column=2, padx=5, pady=5, sticky="e")
cb_red_wifi = ttk.Combobox(
    record_frame,
    values=["2.4 GHz", "5 GHz", "Ethernet", "Otra"],
    state="normal",
    width=12,
)
cb_red_wifi.grid(row=2, column=3, padx=5, pady=5, sticky="w")

ttk.Label(record_frame, text="Notas:").grid(row=2, column=4, padx=5, pady=5, sticky="e")
entry_notas = ttk.Entry(record_frame, width=20, font=("Arial", 10))
entry_notas.grid(row=2, column=5, padx=5, pady=5, sticky="w")
entry_notas.insert(0, "")

rec_status_label = ttk.Label(
    record_frame,
    text="Configura los parámetros e inicia la captura",
    font=("Arial", 10, "bold"),
    foreground="gray",
)
rec_status_label.grid(row=3, column=0, columnspan=6, pady=5)

timestamp_label = ttk.Label(root, text="Timestamp ESP32: 0 ms | Formato: N/A")
timestamp_label.pack()

samples_label = ttk.Label(root, text="Muestras Recibidas: 0")
samples_label.pack(pady=(0, 5))

top_frame = ttk.Frame(root)
top_frame.pack(pady=5)

acc_frame = ttk.LabelFrame(top_frame, text="ACELERÓMETRO (m/s²)")
acc_frame.grid(row=0, column=0, padx=5, ipadx=5, sticky="n")
ttk.Label(acc_frame, text="AX").grid(row=0, column=0, padx=5, pady=2)
value_ax = ttk.Label(acc_frame, text="0.00", font=("Arial", 12, "bold"))
value_ax.grid(row=0, column=1, padx=8)
ttk.Label(acc_frame, text="AY").grid(row=1, column=0, padx=5, pady=2)
value_ay = ttk.Label(acc_frame, text="0.00", font=("Arial", 12, "bold"))
value_ay.grid(row=1, column=1, padx=8)
ttk.Label(acc_frame, text="AZ").grid(row=2, column=0, padx=5, pady=2)
value_az = ttk.Label(acc_frame, text="0.00", font=("Arial", 12, "bold"))
value_az.grid(row=2, column=1, padx=8)

gyro_frame = ttk.LabelFrame(top_frame, text="GIROSCOPIO (deg/s)")
gyro_frame.grid(row=0, column=1, padx=5, ipadx=5, sticky="n")
ttk.Label(gyro_frame, text="GX").grid(row=0, column=0, padx=5, pady=2)
value_gx = ttk.Label(gyro_frame, text="0.00", font=("Arial", 12, "bold"))
value_gx.grid(row=0, column=1, padx=8)
ttk.Label(gyro_frame, text="GY").grid(row=1, column=0, padx=5, pady=2)
value_gy = ttk.Label(gyro_frame, text="0.00", font=("Arial", 12, "bold"))
value_gy.grid(row=1, column=1, padx=8)
ttk.Label(gyro_frame, text="GZ").grid(row=2, column=0, padx=5, pady=2)
value_gz = ttk.Label(gyro_frame, text="0.00", font=("Arial", 12, "bold"))
value_gz.grid(row=2, column=1, padx=8)

net_frame = ttk.LabelFrame(top_frame, text="MÉTRICAS RED (PC / BROKER)")
net_frame.grid(row=0, column=2, padx=5, ipadx=5, sticky="n")
ttk.Label(net_frame, text="Pérdida Datos:").grid(row=0, column=0, padx=5, pady=1, sticky="w")
metric_loss = ttk.Label(net_frame, text="0.00%", font=("Arial", 10, "bold"), foreground="blue")
metric_loss.grid(row=0, column=1, padx=5, sticky="e")
ttk.Label(net_frame, text="Latencia Tránsito:").grid(row=1, column=0, padx=5, pady=1, sticky="w")
metric_latency = ttk.Label(net_frame, text="0.0 ms", font=("Arial", 10, "bold"), foreground="blue")
metric_latency.grid(row=1, column=1, padx=5, sticky="e")
ttk.Label(net_frame, text="Jitter:").grid(row=2, column=0, padx=5, pady=1, sticky="w")
metric_jitter = ttk.Label(net_frame, text="0.00 ms", font=("Arial", 10, "bold"), foreground="blue")
metric_jitter.grid(row=2, column=1, padx=5, sticky="e")
ttk.Label(net_frame, text="Frecuencia Real:").grid(row=0, column=2, padx=5, pady=1, sticky="w")
metric_hz = ttk.Label(net_frame, text="0.0 Hz", font=("Arial", 10, "bold"), foreground="blue")
metric_hz.grid(row=0, column=3, padx=5, sticky="e")
ttk.Label(net_frame, text="Ancho Banda:").grid(row=1, column=2, padx=5, pady=1, sticky="w")
metric_bw = ttk.Label(net_frame, text="0.00 KB/s", font=("Arial", 10, "bold"), foreground="blue")
metric_bw.grid(row=1, column=3, padx=5, sticky="e")
ttk.Label(net_frame, text="Intervalo Llegada:").grid(row=2, column=2, padx=5, pady=1, sticky="w")
metric_interval = ttk.Label(net_frame, text="0.0 ms", font=("Arial", 10, "bold"), foreground="blue")
metric_interval.grid(row=2, column=3, padx=5, sticky="e")

dsp_frame = ttk.LabelFrame(top_frame, text="DSP & LATENCIAS LOCALES ESP32")
dsp_frame.grid(row=0, column=3, padx=5, ipadx=5, sticky="n")
ttk.Label(dsp_frame, text="Frec. Pico:").grid(row=0, column=0, padx=5, pady=1, sticky="w")
value_freq_peak = ttk.Label(dsp_frame, text="0.00 Hz", font=("Arial", 10, "bold"), foreground="purple")
value_freq_peak.grid(row=0, column=1, padx=5, sticky="e")
ttk.Label(dsp_frame, text="PSD Pico:").grid(row=1, column=0, padx=5, pady=1, sticky="w")
value_psd_peak = ttk.Label(dsp_frame, text="0.00e+00", font=("Arial", 10, "bold"), foreground="purple")
value_psd_peak.grid(row=1, column=1, padx=5, sticky="e")
ttk.Label(dsp_frame, text="Energía 4-7Hz:").grid(row=2, column=0, padx=5, pady=1, sticky="w")
value_energy_4_7 = ttk.Label(dsp_frame, text="0.00e+00", font=("Arial", 10, "bold"), foreground="purple")
value_energy_4_7.grid(row=2, column=1, padx=5, sticky="e")
ttk.Label(dsp_frame, text="Energía 2-10Hz:").grid(row=0, column=2, padx=5, pady=1, sticky="w")
value_energy_2_10 = ttk.Label(dsp_frame, text="0.00e+00", font=("Arial", 10, "bold"), foreground="purple")
value_energy_2_10.grid(row=0, column=3, padx=5, sticky="e")
ttk.Label(dsp_frame, text="Ratio Bandas:").grid(row=1, column=2, padx=5, pady=1, sticky="w")
value_band_ratio = ttk.Label(dsp_frame, text="0.0000", font=("Arial", 10, "bold"), foreground="purple")
value_band_ratio.grid(row=1, column=3, padx=5, sticky="e")
ttk.Label(dsp_frame, text="Lat. Pub ESP32:").grid(row=2, column=2, padx=5, pady=1, sticky="w")
value_esp32_pub = ttk.Label(dsp_frame, text="0 µs", font=("Arial", 10, "bold"), foreground="darkgreen")
value_esp32_pub.grid(row=2, column=3, padx=5, sticky="e")
ttk.Label(dsp_frame, text="Lat. Conn ESP32:").grid(row=3, column=0, padx=5, pady=1, sticky="w")
value_esp32_conn = ttk.Label(dsp_frame, text="0 µs", font=("Arial", 10, "bold"), foreground="darkgreen")
value_esp32_conn.grid(row=3, column=1, columnspan=3, padx=5, sticky="w")

figure = Figure(figsize=(14, 5.8), dpi=90)
figure.subplots_adjust(hspace=0.35, wspace=0.25)

ax_plot = figure.add_subplot(221)
ax_plot.set_title("Acelerómetro (m/s²)", fontsize=10, fontweight="bold")
ax_plot.set_ylabel("m/s²", fontsize=8)
ax_plot.grid(True, linestyle="--", alpha=0.6)
(line_ax,) = ax_plot.plot([], [], label="AX", color="blue", lw=1)
(line_ay,) = ax_plot.plot([], [], label="AY", color="orange", lw=1)
(line_az,) = ax_plot.plot([], [], label="AZ", color="green", lw=1)
ax_plot.legend(loc="upper right", fontsize=7)

gyro_plot = figure.add_subplot(222)
gyro_plot.set_title("Giroscopio (deg/s)", fontsize=10, fontweight="bold")
gyro_plot.set_xlabel("Tiempo (s)", fontsize=8)
gyro_plot.set_ylabel("deg/s", fontsize=8)
gyro_plot.grid(True, linestyle="--", alpha=0.6)
(line_gx,) = gyro_plot.plot([], [], label="GX", color="red", lw=1)
(line_gy,) = gyro_plot.plot([], [], label="GY", color="purple", lw=1)
(line_gz,) = gyro_plot.plot([], [], label="GZ", color="brown", lw=1)
gyro_plot.legend(loc="upper right", fontsize=7)

scatter_plot = figure.add_subplot(223)
scatter_plot.set_title("Mapeo: freq_peak vs psd_peak", fontsize=10, fontweight="bold")
scatter_plot.set_xlabel("Freq. Pico (Hz)", fontsize=8)
scatter_plot.set_ylabel("PSD Pico", fontsize=8)
scatter_plot.set_xlim(0, 12)
scatter_plot.grid(True, linestyle="--", alpha=0.6)
scatter_plot.axvspan(4, 6, color="red", alpha=0.15, label="Parkinson (4-6 Hz)")
scatter_plot.axvspan(6, 9, color="orange", alpha=0.15, label="Esencial (6-9 Hz)")
(line_scatter_hist,) = scatter_plot.plot([], [], "o", color="gray", alpha=0.4, markersize=4, label="Historial")
(line_scatter_curr,) = scatter_plot.plot([], [], "o", color="lime", markeredgecolor="black", markersize=8, label="Actual")
scatter_plot.legend(loc="upper left", fontsize=6)

phase_plot = figure.add_subplot(224)
phase_plot.set_title("Espacio de Fases (AX vs GY)", fontsize=10, fontweight="bold")
phase_plot.set_xlabel("AX (m/s²)", fontsize=8)
phase_plot.set_ylabel("GY (deg/s)", fontsize=8)
phase_plot.set_xlim(-25, 25)
phase_plot.grid(True, linestyle="--", alpha=0.6)
(line_phase,) = phase_plot.plot([], [], color="magenta", alpha=0.6, lw=0.8, label="Órbita")
(line_phase_curr,) = phase_plot.plot([], [], "ro", markersize=6, label="Actual")
phase_plot.legend(loc="upper right", fontsize=7)

canvas = FigureCanvasTkAgg(figure, master=root)
canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

start_mqtt_client()
update_values()
update_plot()

root.mainloop()