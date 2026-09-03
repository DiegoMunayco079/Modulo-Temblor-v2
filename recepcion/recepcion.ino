#include <Wire.h>
#include <SPI.h>
#include <SD.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <cbor.h>
#include "DFRobot_BNO055.h"

#define USE_CBOR false

typedef DFRobot_BNO055_IIC BNO;
BNO bno(&Wire, 0x28);

#define SDA_PIN 6
#define SCL_PIN 7
#define BTN_START_STOP 4
#define SD_CS 10
#define SD_SCK 18
#define SD_MOSI 19
#define SD_MISO 20
#define LED_IOT 15

SPIClass spi(FSPI);

const char* WIFI_SSID = "Diego";
const char* WIFI_PASS = "diego079";
const char* MQTT_BROKER = "10.247.51.122";
const int MQTT_PORT = 1883;

const char* MQTT_TOPIC_JSON = "temblores/wearable_t2/json";
const char* MQTT_TOPIC_CBOR = "temblores/wearable_t2/cbor";

WiFiClient espClient;
PubSubClient mqtt(espClient);

#define FS 100.0
#define SAMPLE_PERIOD_US 10000UL
#define WINDOW_SIZE 256
#define MQTT_BATCH_SIZE 10

uint32_t sampleNumber = 0;
unsigned long lastSampleMicros = 0;
unsigned long previousSampleMicros = 0;

uint32_t fsCount = 0;
double fsSum = 0.0;
double fsSumSquared = 0.0;
double fsMin = 999999.0;
double fsMax = 0.0;

uint32_t processingCount = 0;
double processingSum = 0.0;
double processingSumSquared = 0.0;
double processingMin = 999999.0;
double processingMax = 0.0;

float signalBuffer[WINDOW_SIZE];
float filteredBuffer[WINDOW_SIZE];
uint16_t bufferIndex = 0;

char sdCharBuffer[160];

const double SOS[4][6] = {
    {0.0022348917, 0.0044697834, 0.0022348917, 1.0, -1.3371336715, 0.4967223217},
    {1.0, 2.0, 1.0, 1.0, -1.4222772942, 0.7371397447},
    {1.0, -2.0, 1.0, 1.0, -1.7469558080, 0.7730761142},
    {1.0, -2.0, 1.0, 1.0, -1.9181020531, 0.9342556252}
};

double z1[4] = {0, 0, 0, 0};
double z2[4] = {0, 0, 0, 0};

double fftReal[WINDOW_SIZE];
double fftImag[WINDOW_SIZE];

File dataFile;
bool recording = false;
bool lastButtonState = HIGH;

uint16_t recordingCounter = 1;
String currentFilename = "";

float lastAx = 0;
float lastAy = 0;
float lastAz = 0;

float lastGx = 0;
float lastGy = 0;
float lastGz = 0;

float dominantFrequency = 0;
double peakPSD = 0;
double energy4_7 = 0;
double energy2_10 = 0;
double bandRatio = 0;

unsigned long mqttPublishLatency = 0;
uint32_t mqttMessagesSent = 0;
uint32_t mqttPublishErrors = 0;

struct MQTT_Sample {
    uint32_t timestamp;
    float ax;
    float ay;
    float az;
    float gx;
    float gy;
    float gz;
};

MQTT_Sample mqttBuffer[MQTT_BATCH_SIZE];
uint8_t mqttBufferIndex = 0;

void waitCalibration();
String generateFilename();
void startRecording();
void stopRecording();
void connectWiFi();
void reconnectMQTT();
float applyButterworth(float input);
void fft(double* real, double* imag, int n);
void analyzeTremor();
void sendMQTTBatch();
void printStatistics();

String generateFilename() {
    while (true) {
        char filename[30];
        sprintf(filename, "/P%03d_data.csv", recordingCounter);

        if (!SD.exists(filename))
            return String(filename);

        recordingCounter++;
    }
}

void startRecording() {
    currentFilename = generateFilename();

    dataFile = SD.open(
        currentFilename.c_str(),
        FILE_WRITE
    );

    if (!dataFile) {
        Serial.println("[ERROR] No se pudo crear el archivo en la MicroSD.");
        return;
    }

    dataFile.println("timestamp_ms,ax,ay,az,gx,gy,gz");

    sampleNumber = 0;
    bufferIndex = 0;
    mqttBufferIndex = 0;

    fsCount = 0;
    previousSampleMicros = 0;
    fsSum = 0.0;
    fsSumSquared = 0.0;
    fsMin = 999999.0;
    fsMax = 0.0;

    processingCount = 0;
    processingSum = 0.0;
    processingSumSquared = 0.0;
    processingMin = 999999.0;
    processingMax = 0.0;

    mqttMessagesSent = 0;
    mqttPublishErrors = 0;
    mqttPublishLatency = 0;

    for (int i = 0; i < 4; i++) {
        z1[i] = 0;
        z2[i] = 0;
    }

    lastSampleMicros = micros();
    recording = true;

    Serial.println();
    Serial.println("================================================");
    Serial.print("GRABACION Y TRANSMISION INICIADAS: ");
    Serial.println(currentFilename);
    Serial.println("================================================");
}

void stopRecording() {
    recording = false;

    if (mqttBufferIndex > 0) {
        sendMQTTBatch();
        mqttBufferIndex = 0;
    }

    if (dataFile) {
        dataFile.flush();
        dataFile.close();
    }

    Serial.println();
    Serial.println("================================================");
    Serial.println("GRABACION Y TRANSMISION DETENIDAS");
    Serial.println("================================================");

    printStatistics();

    recordingCounter++;
}

void waitCalibration() {
    DFRobot_BNO055::sRegCalibState_t cal;
    uint8_t intentos = 0;

    while (intentos < 10) {
        cal = bno.getCalStatus();

        Serial.printf(
            "[BNO055] ACC: %d, GYR: %d\n",
            cal.ACC,
            cal.GYR
        );

        if (cal.ACC >= 0 && cal.GYR >= 2)
            break;

        delay(1000);
        intentos++;
    }
}

void connectWiFi() {
    WiFi.begin(WIFI_SSID, WIFI_PASS);

    while (WiFi.status() != WL_CONNECTED) {
        delay(500);
        Serial.print(".");
    }

    Serial.println("\n[Wi-Fi] Conectado.");
    Serial.print("[Wi-Fi] IP: ");
    Serial.println(WiFi.localIP());
}

void reconnectMQTT() {
    while (!mqtt.connected()) {
        digitalWrite(LED_IOT, LOW);

        String clientId =
            "ESP32_T2_" +
            String((uint32_t)ESP.getEfuseMac(), HEX);

        if (mqtt.connect(clientId.c_str())) {
            digitalWrite(LED_IOT, HIGH);
            Serial.println("[MQTT] Conectado.");
        } else {
            Serial.print("[MQTT] Error rc=");
            Serial.println(mqtt.state());
            delay(2000);
        }
    }
}

float applyButterworth(float input) {
    double output = input;

    for (int s = 0; s < 4; s++) {
        double b0 = SOS[s][0];
        double b1 = SOS[s][1];
        double b2 = SOS[s][2];
        double a1 = SOS[s][4];
        double a2 = SOS[s][5];

        double y = b0 * output + z1[s];

        z1[s] =
            b1 * output -
            a1 * y +
            z2[s];

        z2[s] =
            b2 * output -
            a2 * y;

        output = y;
    }

    return (float)output;
}

void fft(double* real, double* imag, int n) {
    int j = 0;

    for (int i = 1; i < n; i++) {
        int bit = n >> 1;

        while (j & bit) {
            j ^= bit;
            bit >>= 1;
        }

        j ^= bit;

        if (i < j) {
            double temp;

            temp = real[i];
            real[i] = real[j];
            real[j] = temp;

            temp = imag[i];
            imag[i] = imag[j];
            imag[j] = temp;
        }
    }

    for (int len = 2; len <= n; len <<= 1) {
        double angle = -2.0 * PI / len;
        double wlenReal = cos(angle);
        double wlenImag = sin(angle);

        for (int i = 0; i < n; i += len) {
            double wReal = 1.0;
            double wImag = 0.0;

            for (int k = 0; k < len / 2; k++) {
                int u = i + k;
                int v = i + k + len / 2;

                double vReal =
                    real[v] * wReal -
                    imag[v] * wImag;

                double vImag =
                    real[v] * wImag +
                    imag[v] * wReal;

                double uReal = real[u];
                double uImag = imag[u];

                real[u] = uReal + vReal;
                imag[u] = uImag + vImag;

                real[v] = uReal - vReal;
                imag[v] = uImag - vImag;

                double nextWReal =
                    wReal * wlenReal -
                    wImag * wlenImag;

                double nextWImag =
                    wReal * wlenImag +
                    wImag * wlenReal;

                wReal = nextWReal;
                wImag = nextWImag;
            }
        }
    }
}

void analyzeTremor() {
    unsigned long processingStart = micros();

    double meanValue = 0;

    for (int i = 0; i < WINDOW_SIZE; i++)
        meanValue += signalBuffer[i];

    meanValue /= WINDOW_SIZE;

    for (int i = 0; i < WINDOW_SIZE; i++) {
        filteredBuffer[i] =
            applyButterworth(
                signalBuffer[i] - meanValue
            );
    }

    double windowEnergy = 0;

    for (int i = 0; i < WINDOW_SIZE; i++) {
        double w =
            0.5 *
            (1.0 - cos(
                2.0 * PI * i /
                (WINDOW_SIZE - 1)
            ));

        fftReal[i] =
            filteredBuffer[i] * w;

        fftImag[i] = 0;

        windowEnergy += w * w;
    }

    fft(
        fftReal,
        fftImag,
        WINDOW_SIZE
    );

    double frequencyResolution =
        FS / WINDOW_SIZE;

    dominantFrequency = 0;
    peakPSD = 0;
    energy4_7 = 0;
    energy2_10 = 0;

    for (int k = 0; k <= WINDOW_SIZE / 2; k++) {
        double magnitudeSquared =
            fftReal[k] * fftReal[k] +
            fftImag[k] * fftImag[k];

        double psd =
            magnitudeSquared /
            (FS * windowEnergy);

        if (k > 0 && k < WINDOW_SIZE / 2)
            psd *= 2.0;

        double frequency =
            k * frequencyResolution;

        if (frequency >= 2.0 &&
            frequency <= 10.0) {

            if (psd > peakPSD) {
                peakPSD = psd;
                dominantFrequency = frequency;
            }
        }

        if (frequency >= 4.0 &&
            frequency <= 7.0) {

            energy4_7 +=
                psd * frequencyResolution;
        }

        if (frequency >= 2.0 &&
            frequency <= 10.0) {

            energy2_10 +=
                psd * frequencyResolution;
        }
    }

    bandRatio =
        energy2_10 > 0 ?
        energy4_7 / energy2_10 :
        0;

    unsigned long processingTime =
        micros() - processingStart;

    double processingMs =
        processingTime / 1000.0;

    processingSum += processingMs;
    processingSumSquared +=
        processingMs * processingMs;

    if (processingMs < processingMin)
        processingMin = processingMs;

    if (processingMs > processingMax)
        processingMax = processingMs;

    processingCount++;

    bufferIndex = 0;

    Serial.printf(
        "[FFT] Pico: %.3f Hz | Ratio: %.4f | Proc: %.2f ms\n",
        dominantFrequency,
        bandRatio,
        processingMs
    );
}

void sendMQTTBatch() {
    if (mqttBufferIndex == 0)
        return;

    if (!mqtt.connected())
        reconnectMQTT();

    if (!mqtt.connected())
        return;

    unsigned long t0 = micros();
    bool success = false;
    size_t payloadSize = 0;

#if USE_CBOR

    uint8_t buffer[1200];

    CborEncoder encoder;
    CborEncoder mapEncoder;
    CborEncoder arrayEncoder;

    cbor_encoder_init(
        &encoder,
        buffer,
        sizeof(buffer),
        0
    );

    cbor_encoder_create_map(
        &encoder,
        &mapEncoder,
        8
    );

    cbor_encode_text_stringz(
        &mapEncoder,
        "seq"
    );

    cbor_encode_uint(
        &mapEncoder,
        sampleNumber
    );

    cbor_encode_text_stringz(
        &mapEncoder,
        "n"
    );

    cbor_encode_uint(
        &mapEncoder,
        mqttBufferIndex
    );

    cbor_encode_text_stringz(
        &mapEncoder,
        "tx_time_ms"
    );

    cbor_encode_uint(
        &mapEncoder,
        (unsigned long)millis()
    );

    cbor_encode_text_stringz(
        &mapEncoder,
        "freq_peak"
    );

    cbor_encode_float(
        &mapEncoder,
        dominantFrequency
    );

    cbor_encode_text_stringz(
        &mapEncoder,
        "psd_peak"
    );

    cbor_encode_float(
        &mapEncoder,
        (float)peakPSD
    );

    cbor_encode_text_stringz(
        &mapEncoder,
        "energy_4_7"
    );

    cbor_encode_float(
        &mapEncoder,
        (float)energy4_7
    );

    cbor_encode_text_stringz(
        &mapEncoder,
        "energy_2_10"
    );

    cbor_encode_float(
        &mapEncoder,
        (float)energy2_10
    );

    cbor_encode_text_stringz(
        &mapEncoder,
        "band_ratio"
    );

    cbor_encode_float(
        &mapEncoder,
        (float)bandRatio
    );

    cbor_encode_text_stringz(
        &mapEncoder,
        "samples"
    );

    cbor_encoder_create_array(
        &mapEncoder,
        &arrayEncoder,
        mqttBufferIndex
    );

    for (int i = 0; i < mqttBufferIndex; i++) {

        CborEncoder sampleMap;

        cbor_encoder_create_map(
            &arrayEncoder,
            &sampleMap,
            7
        );

        cbor_encode_text_stringz(
            &sampleMap,
            "t"
        );

        cbor_encode_uint(
            &sampleMap,
            mqttBuffer[i].timestamp
        );

        cbor_encode_text_stringz(
            &sampleMap,
            "ax"
        );

        cbor_encode_float(
            &sampleMap,
            mqttBuffer[i].ax
        );

        cbor_encode_text_stringz(
            &sampleMap,
            "ay"
        );

        cbor_encode_float(
            &sampleMap,
            mqttBuffer[i].ay
        );

        cbor_encode_text_stringz(
            &sampleMap,
            "az"
        );

        cbor_encode_float(
            &sampleMap,
            mqttBuffer[i].az
        );

        cbor_encode_text_stringz(
            &sampleMap,
            "gx"
        );

        cbor_encode_float(
            &sampleMap,
            mqttBuffer[i].gx
        );

        cbor_encode_text_stringz(
            &sampleMap,
            "gy"
        );

        cbor_encode_float(
            &sampleMap,
            mqttBuffer[i].gy
        );

        cbor_encode_text_stringz(
            &sampleMap,
            "gz"
        );

        cbor_encode_float(
            &sampleMap,
            mqttBuffer[i].gz
        );

        cbor_encoder_close_container(
            &arrayEncoder,
            &sampleMap
        );
    }

    cbor_encoder_close_container(
        &mapEncoder,
        &arrayEncoder
    );

    cbor_encoder_close_container(
        &encoder,
        &mapEncoder
    );

    payloadSize =
        cbor_encoder_get_buffer_size(
            &encoder,
            buffer
        );

    success =
        mqtt.publish(
            MQTT_TOPIC_CBOR,
            buffer,
            payloadSize
        );

#else

    char payload[1400];

    int offset = 0;

    unsigned long txMs = millis();

    offset += snprintf(
        payload + offset,
        sizeof(payload) - offset,
        "{"
        "\"seq\":%lu,"
        "\"n\":%u,"
        "\"tx_time_ms\":%lu,"
        "\"freq_peak\":%.3f,"
        "\"psd_peak\":%.8e,"
        "\"energy_4_7\":%.8e,"
        "\"energy_2_10\":%.8e,"
        "\"band_ratio\":%.5f,"
        "\"samples\":[",
        (unsigned long)sampleNumber,
        mqttBufferIndex,
        txMs,
        dominantFrequency,
        peakPSD,
        energy4_7,
        energy2_10,
        bandRatio
    );

    for (int i = 0; i < mqttBufferIndex; i++) {

        if (i > 0) {
            offset += snprintf(
                payload + offset,
                sizeof(payload) - offset,
                ","
            );
        }

        offset += snprintf(
            payload + offset,
            sizeof(payload) - offset,

            "{"
            "\"t\":%lu,"
            "\"ax\":%.3f,"
            "\"ay\":%.3f,"
            "\"az\":%.3f,"
            "\"gx\":%.3f,"
            "\"gy\":%.3f,"
            "\"gz\":%.3f"
            "}",

            (unsigned long)mqttBuffer[i].timestamp,

            mqttBuffer[i].ax,
            mqttBuffer[i].ay,
            mqttBuffer[i].az,

            mqttBuffer[i].gx,
            mqttBuffer[i].gy,
            mqttBuffer[i].gz
        );
    }

    offset += snprintf(
        payload + offset,
        sizeof(payload) - offset,
        "]}"
    );

    payloadSize = offset;

    success =
        mqtt.publish(
            MQTT_TOPIC_JSON,
            (uint8_t*)payload,
            payloadSize
        );

#endif

    mqttPublishLatency =
        micros() - t0;

    if (success)
        mqttMessagesSent++;
    else
        mqttPublishErrors++;

    Serial.printf(
        "[MQTT] %s | Paquete: %u muestras | %d B | %lu us\n",
        success ? "OK" : "ERROR",
        mqttBufferIndex,
        (int)payloadSize,
        mqttPublishLatency
    );
}

void printStatistics() {
    Serial.println();
    Serial.println("---------------- ESTADISTICAS ----------------");

    if (fsCount > 0) {
        double meanFs =
            fsSum / fsCount;

        double variance =
            fsSumSquared / fsCount -
            meanFs * meanFs;

        if (variance < 0)
            variance = 0;

        Serial.printf(
            "[FS] Promedio: %.3f Hz\n",
            meanFs
        );

        Serial.printf(
            "[FS] Min: %.3f Hz\n",
            fsMin
        );

        Serial.printf(
            "[FS] Max: %.3f Hz\n",
            fsMax
        );

        Serial.printf(
            "[FS] Desviacion: %.3f Hz\n",
            sqrt(variance)
        );
    }

    if (processingCount > 0) {
        double meanProcessing =
            processingSum /
            processingCount;

        double variance =
            processingSumSquared /
            processingCount -
            meanProcessing *
            meanProcessing;

        if (variance < 0)
            variance = 0;

        Serial.printf(
            "[FFT] Promedio: %.3f ms\n",
            meanProcessing
        );

        Serial.printf(
            "[FFT] Min: %.3f ms\n",
            processingMin
        );

        Serial.printf(
            "[FFT] Max: %.3f ms\n",
            processingMax
        );

        Serial.printf(
            "[FFT] Desviacion: %.3f ms\n",
            sqrt(variance)
        );
    }

    Serial.printf(
        "[MQTT] Paquetes enviados: %lu\n",
        (unsigned long)mqttMessagesSent
    );

    Serial.printf(
        "[MQTT] Errores: %lu\n",
        (unsigned long)mqttPublishErrors
    );

    Serial.println(
        "------------------------------------------------"
    );
}

void setup() {
    Serial.begin(115200);
    delay(1000);

    Serial.println();
    Serial.println(
        "--------------------------------------------------"
    );
    Serial.println(
        "       INICIANDO ESP32-C6 PARKINSON T2"
    );
    Serial.println(
        "--------------------------------------------------"
    );

    pinMode(
        BTN_START_STOP,
        INPUT_PULLUP
    );

    Wire.begin(
        SDA_PIN,
        SCL_PIN
    );

    Wire.setClock(400000);

    Serial.print(
        "[BNO055] Inicializando en 0x28... "
    );

    if (bno.begin() != BNO::eStatusOK) {
        Serial.println(
            "ERROR! No se detecto el BNO055."
        );

        while (1)
            delay(500);
    }

    Serial.println("OK");

    waitCalibration();

    Serial.print(
        "[SD] Inicializando interfaz SPI... "
    );

    spi.begin(
        SD_SCK,
        SD_MISO,
        SD_MOSI,
        SD_CS
    );

    if (!SD.begin(
        SD_CS,
        spi,
        20000000
    )) {

        Serial.println(
            "ERROR! Fallo al montar tarjeta SD."
        );

        while (1)
            delay(500);
    }

    Serial.println("OK");

    pinMode(
        LED_IOT,
        OUTPUT
    );

    digitalWrite(
        LED_IOT,
        LOW
    );

    Serial.print(
        "[Wi-Fi] Conectando a "
    );

    Serial.println(
        WIFI_SSID
    );

    connectWiFi();

    mqtt.setServer(
        MQTT_BROKER,
        MQTT_PORT
    );

    mqtt.setBufferSize(2048);

    Serial.print(
        "[MQTT] Conectando a "
    );

    Serial.println(
        MQTT_BROKER
    );

    reconnectMQTT();

    Serial.println();
    Serial.println(
        "=================================================="
    );
    Serial.println(
        "      >>> ESP32-C6 INICIALIZADO Y LISTO <<<"
    );
    Serial.println(
        "=================================================="
    );

    Serial.printf(
        "[CONFIG] Adquisicion: %.0f Hz\n",
        FS
    );

    Serial.printf(
        "[CONFIG] FFT: %d muestras / %.2f s\n",
        WINDOW_SIZE,
        WINDOW_SIZE / FS
    );

    Serial.printf(
        "[CONFIG] MQTT: %.1f mensajes/s\n",
        FS / MQTT_BATCH_SIZE
    );

    Serial.printf(
        "[CONFIG] MQTT: %d muestras/paquete\n",
        MQTT_BATCH_SIZE
    );
}

void loop() {

    if (!mqtt.connected())
        reconnectMQTT();

    mqtt.loop();

    bool currentButtonState =
        digitalRead(BTN_START_STOP);

    if (
        lastButtonState == HIGH &&
        currentButtonState == LOW
    ) {

        delay(50);

        if (
            digitalRead(BTN_START_STOP) == LOW
        ) {

            Serial.println(
                "\n>>> BOTON PRESIONADO <<<"
            );

            if (recording)
                stopRecording();
            else
                startRecording();
        }
    }

    lastButtonState =
        currentButtonState;

    if (!recording)
        return;

    unsigned long currentMicros =
        micros();

    if (
        (unsigned long)(
            currentMicros -
            lastSampleMicros
        )
        < SAMPLE_PERIOD_US
    ) {
        return;
    }

    lastSampleMicros +=
        SAMPLE_PERIOD_US;

    if (
        (unsigned long)(
            currentMicros -
            lastSampleMicros
        )
        > SAMPLE_PERIOD_US
    ) {

        lastSampleMicros =
            currentMicros;
    }

    if (previousSampleMicros != 0) {

        unsigned long deltaUs =
            currentMicros -
            previousSampleMicros;

        if (deltaUs > 0) {

            double frequency =
                1000000.0 /
                deltaUs;

            fsSum += frequency;

            fsSumSquared +=
                frequency *
                frequency;

            if (frequency < fsMin)
                fsMin = frequency;

            if (frequency > fsMax)
                fsMax = frequency;

            fsCount++;
        }
    }

    previousSampleMicros =
        currentMicros;

    BNO::sAxisAnalog_t acc =
        bno.getAxis(BNO::eAxisAcc);

    BNO::sAxisAnalog_t gyr =
        bno.getAxis(BNO::eAxisGyr);

    float ax = acc.x / 1000.0;
    float ay = acc.y / 1000.0;
    float az = acc.z / 1000.0;

    float gx = gyr.x;
    float gy = gyr.y;
    float gz = gyr.z;

    float magnitude =
        sqrt(
            ax * ax +
            ay * ay +
            az * az
        );

    uint32_t timestamp =
        currentMicros / 1000UL;

    signalBuffer[bufferIndex] =
        magnitude;

    mqttBuffer[mqttBufferIndex].timestamp =
        timestamp;

    mqttBuffer[mqttBufferIndex].ax =
        ax;

    mqttBuffer[mqttBufferIndex].ay =
        ay;

    mqttBuffer[mqttBufferIndex].az =
        az;

    mqttBuffer[mqttBufferIndex].gx =
        gx;

    mqttBuffer[mqttBufferIndex].gy =
        gy;

    mqttBuffer[mqttBufferIndex].gz =
        gz;

    mqttBufferIndex++;

    if (dataFile) {

        snprintf(
            sdCharBuffer,
            sizeof(sdCharBuffer),

            "%lu,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f\n",

            (unsigned long)timestamp,

            ax,
            ay,
            az,

            gx,
            gy,
            gz
        );

        dataFile.print(
            sdCharBuffer
        );
    }

    sampleNumber++;

    if (
        mqttBufferIndex >=
        MQTT_BATCH_SIZE
    ) {

        sendMQTTBatch();

        mqttBufferIndex = 0;
    }

    bufferIndex++;

    if (
        bufferIndex >=
        WINDOW_SIZE
    ) {

        analyzeTremor();
    }
}