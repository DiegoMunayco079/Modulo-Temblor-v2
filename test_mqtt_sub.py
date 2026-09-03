"""Suscriptor de prueba: verifica si el ESP32 publica datos por MQTT.
Uso: python test_mqtt_sub.py [ip_broker]
"""
import sys
import time

import cbor2
import paho.mqtt.client as mqtt

BROKER = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
PORT = 1883
COUNTER = {"n": 0, "cbor": 0, "json": 0}


def on_connect(client, userdata, flags, rc, properties=None):
    print(f"[i] Conectado al broker {BROKER} (rc={rc})")
    client.subscribe("temblores/wearable_t2/#", qos=0)
    print("[i] Suscrito a temblores/wearable_t2/# ... esperando datos")


def on_message(client, userdata, msg):
    COUNTER["n"] += 1
    data = None
    try:
        if msg.topic.endswith("/cbor"):
            data = cbor2.loads(msg.payload)
            COUNTER["cbor"] += 1
        elif msg.topic.endswith("/json"):
            data = msg.payload.decode("utf-8")
            COUNTER["json"] += 1
    except Exception as e:
        print("Error decodificando:", e)
        return

    if COUNTER["n"] <= 5:
        print(f"[{COUNTER['n']}] TOPIC: {msg.topic} | Bytes: {len(msg.payload)}")
        print("     PAYLOAD:", data)
    if COUNTER["n"] % 50 == 0:
        print(f"[i] Recibidos: {COUNTER['n']} | CBOR: {COUNTER['cbor']} | JSON: {COUNTER['json']}")


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.on_connect = on_connect
client.on_message = on_message

client.connect(BROKER, PORT, keepalive=60)
client.loop_start()

print(f"[i] Escuchando en {BROKER}:{PORT}. Presiona Ctrl+C para salir.")
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("\n[i] Resumen final:")
    print(f"    Total recibidos: {COUNTER['n']}")
    print(f"    CBOR: {COUNTER['cbor']} | JSON: {COUNTER['json']}")
    client.disconnect()
