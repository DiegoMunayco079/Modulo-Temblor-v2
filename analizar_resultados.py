import glob
import os
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# ============================================================
# RUTAS DE ARCHIVOS Y CARPETAS
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_FOLDER = os.path.join(BASE_DIR, "Datos_csv")
OUTPUT_FOLDER = os.path.join(BASE_DIR, "Resultados_Comparativos")

os.makedirs(OUTPUT_FOLDER, exist_ok=True)


def analizar_datos():
    csv_files = glob.glob(os.path.join(CSV_FOLDER, "*.csv"))

    if not csv_files:
        print(f"[x] No se encontraron archivos CSV en la carpeta: {CSV_FOLDER}")
        return

    print(
        f"[i] Cargando y analizando {len(csv_files)} archivo(s) de toma de datos..."
    )

    # Cargar y concatenar todos los archivos guardados por monitor_bno055.py
    df_list = []
    for file in csv_files:
        try:
            df_temp = pd.read_csv(file)
            df_list.append(df_temp)
        except Exception as e:
            print(f"[!] Error al leer {file}: {e}")

    if not df_list:
        print("[x] No hay datos válidos para analizar.")
        return

    df = pd.concat(df_list, ignore_index=True)

    # Si alguna columna de condiciones está vacía, rellenar con valor neutro
    for cond in ["distancia_router_m", "tipo_red_wifi", "notas"]:
        if cond not in df.columns:
            df[cond] = "N/A"

    # Limpieza y conversión de columnas numéricas
    cols_to_numeric = [
        "esp32_pub_latency_us",
        "esp32_conn_latency_us",
        "pc_rx_time_ms",
        "tx_time_ms",
    ]
    for col in cols_to_numeric:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Calculo de latencias en milisegundos (ms)
    df["latencia_pub_ms"] = df["esp32_pub_latency_us"] / 1000.0
    df["latencia_conn_ms"] = df["esp32_conn_latency_us"] / 1000.0
    df["latencia_transito_ms"] = np.maximum(
        0, df["pc_rx_time_ms"] - df["tx_time_ms"]
    )

    # ============================================================
    # 1. CÁLCULO DE RESUMEN ESTADÍSTICO
    # ============================================================
    resumen = (
        df.groupby("format")
        .agg(
            Total_Muestras=("seq", "count"),
            Lat_Pub_Prom_ms=("latencia_pub_ms", "mean"),
            Lat_Pub_Std_ms=("latencia_pub_ms", "std"),
            Lat_Conn_Prom_ms=("latencia_conn_ms", "mean"),
            Lat_Transito_Prom_ms=("latencia_transito_ms", "mean"),
            Lat_Transito_Std_ms=("latencia_transito_ms", "std"),
        )
        .reset_index()
    )

    # Porcentaje de reducción/mejora si existen ambos formatos
    if set(["CBOR", "JSON"]).issubset(df["format"].unique()):
        json_pub = resumen.loc[
            resumen["format"] == "JSON", "Lat_Pub_Prom_ms"
        ].values[0]
        cbor_pub = resumen.loc[
            resumen["format"] == "CBOR", "Lat_Pub_Prom_ms"
        ].values[0]

        json_trans = resumen.loc[
            resumen["format"] == "JSON", "Lat_Transito_Prom_ms"
        ].values[0]
        cbor_trans = resumen.loc[
            resumen["format"] == "CBOR", "Lat_Transito_Prom_ms"
        ].values[0]

        ahorro_pub = ((json_pub - cbor_pub) / json_pub) * 100
        ahorro_trans = ((json_trans - cbor_trans) / json_trans) * 100

        print("\n==================================================")
        print("  RESULTADOS COMPARATIVOS (CBOR vs JSON)")
        print("==================================================")
        print(f" Reducción Latencia Publicación (ESP32): {ahorro_pub:.2f}%")
        print(f" Reducción Latencia Tránsito (Red WiFi):  {ahorro_trans:.2f}%")
        print("==================================================\n")

    # Guardar resumen en CSV dentro de la carpeta independiente
    csv_salida = os.path.join(
        OUTPUT_FOLDER, "resumen_estadistico_comparativo.csv"
    )
    resumen.to_csv(csv_salida, index=False)
    print(f"[✓] Tabla de resumen guardada en: {csv_salida}")

    # ============================================================
    # 1b. RESUMEN DE CONDICIONES DE PRUEBA POR FORMATO
    # ============================================================
    condiciones_resumen = (
        df.groupby("format")
        .agg(
            Archivos_Toma=("seq", "count"),
            Distancia_Router_m=("distancia_router_m", "first"),
            Tipo_Red_WiFi=("tipo_red_wifi", "first"),
            Notas=("notas", "first"),
        )
        .reset_index()
    )

    csv_condiciones = os.path.join(
        OUTPUT_FOLDER, "condiciones_de_prueba_por_formato.csv"
    )
    condiciones_resumen.to_csv(csv_condiciones, index=False)
    print(f"[✓] Condiciones de prueba guardadas en: {csv_condiciones}")

    # ============================================================
    # 2. GENERACIÓN DE GRÁFICOS PARA TESIS / INFORME
    # ============================================================
    plt.style.use("seaborn-v0_8-whitegrid" if "seaborn-v0_8-whitegrid" in plt.style.available else "default")

    fig, axes = plt.subplots(1, 2, figsize=(12, 5))

    # Gráfico 1: Latencia de Publicación en ESP32
    df.boxplot(
        column="latencia_pub_ms",
        by="format",
        ax=axes[0],
        patch_artist=True,
        boxprops=dict(facecolor="lightblue"),
    )
    axes[0].set_title("Latencia de Publicación Local ESP32")
    axes[0].set_xlabel("Formato de Datos")
    axes[0].set_ylabel("Milisegundos (ms)")

    # Gráfico 2: Latencia de Tránsito por Red (PC - ESP32)
    df.boxplot(
        column="latencia_transito_ms",
        by="format",
        ax=axes[1],
        patch_artist=True,
        boxprops=dict(facecolor="lightgreen"),
    )
    axes[1].set_title("Latencia de Tránsito Red (Red WiFi / MQTT)")
    axes[1].set_xlabel("Formato de Datos")
    axes[1].set_ylabel("Milisegundos (ms)")

    plt.suptitle("Comparativa de Desempeño Protocolos: CBOR vs JSON", fontsize=14, fontweight="bold")
    plt.tight_layout()

    fig_salida = os.path.join(OUTPUT_FOLDER, "comparativa_latencias_boxplots.png")
    plt.savefig(fig_salida, dpi=300)
    plt.close()

    print(f"[✓] Gráficos estadísticos guardados en: {fig_salida}\n")


if __name__ == "__main__":
    analizar_datos()