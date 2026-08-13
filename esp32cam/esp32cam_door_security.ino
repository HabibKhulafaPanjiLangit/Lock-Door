/*
  Sistem Keamanan Pintu - ESP32-CAM
  ==================================
  Board   : AI-Thinker ESP32-CAM
  Fungsi  :
    1. Otomatis ambil foto setiap CAPTURE_INTERVAL_MS (default 2 detik)
    2. Kirim foto (raw JPEG) via HTTP POST ke server Flask endpoint /verify
    3. Baca respons JSON {"access": true/false, "name": "...", "confidence": ...}
    4. Jika access = true -> aktifkan relay (GPIO_RELAY) untuk membuka solenoid lock

  Library yang dibutuhkan (install via Library Manager Arduino IDE):
    - ArduinoJson (by Benoit Blanchon)
    - (esp_camera.h sudah termasuk di board package ESP32 Arduino Core)

  PENTING - Pemilihan Pin (khusus board AI-Thinker ESP32-CAM):
    Kamera memakai banyak GPIO (0,5,18,19,21,22,23,25,26,27,32,34,35,36,39).
    Karena kita TIDAK memakai SD card, pin 12, 13, 14, 15 bebas dipakai.
    - GPIO_RELAY (output ke relay) = GPIO 13
*/

#include "esp_camera.h"
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>

// ================== KONFIGURASI ==================
const char* WIFI_SSID     = "NAMA_WIFI_ANDA";
const char* WIFI_PASSWORD = "PASSWORD_WIFI_ANDA";

// Ganti dengan IP komputer/server yang menjalankan Flask (app.py)
const char* SERVER_URL = "http://192.168.1.100:5000/verify";

#define GPIO_RELAY     13   // output ke modul relay -> solenoid door lock
#define RELAY_ACTIVE_HIGH true   // ubah ke false kalau modul relay aktif LOW
#define UNLOCK_DURATION_MS 5000  // pintu terbuka selama 5 detik

#define GPIO_FLASH_LED   4   // LED flash bawaan ESP32-CAM, menyala terus selama sistem hidup

// Seberapa sering ESP32-CAM mengambil foto & mengirim ke server (ms).
// Ubah angka ini sesuai kebutuhan, misal 1000 = tiap 1 detik, 5000 = tiap 5 detik.
#define CAPTURE_INTERVAL_MS 1000

// ================== PIN KAMERA (AI-Thinker) ==================
#define PWDN_GPIO_NUM     32
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM      0
#define SIOD_GPIO_NUM     26
#define SIOC_GPIO_NUM     27
#define Y9_GPIO_NUM       35
#define Y8_GPIO_NUM       34
#define Y7_GPIO_NUM       39
#define Y6_GPIO_NUM       36
#define Y5_GPIO_NUM       21
#define Y4_GPIO_NUM       19
#define Y3_GPIO_NUM       18
#define Y2_GPIO_NUM        5
#define VSYNC_GPIO_NUM    25
#define HREF_GPIO_NUM     23
#define PCLK_GPIO_NUM     22

unsigned long lastCaptureTime = 0;

void setRelay(bool unlock) {
  bool level = RELAY_ACTIVE_HIGH ? unlock : !unlock;
  digitalWrite(GPIO_RELAY, level ? HIGH : LOW);
}

bool initCamera() {
  camera_config_t config;
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer   = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM;
  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;
  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;
  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;
  config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM;
  config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href = HREF_GPIO_NUM;
  config.pin_sscb_sda = SIOD_GPIO_NUM;
  config.pin_sscb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn = PWDN_GPIO_NUM;
  config.pin_reset = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;

  if (psramFound()) {
    config.frame_size = FRAMESIZE_VGA; // 640x480, cukup untuk face recognition
    config.jpeg_quality = 12;          // makin kecil makin bagus kualitasnya
    config.fb_count = 2;
  } else {
    config.frame_size = FRAMESIZE_QVGA;
    config.jpeg_quality = 15;
    config.fb_count = 1;
  }

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("Inisialisasi kamera gagal, kode error: 0x%x\n", err);
    return false;
  }

  // ===== Optimasi sensor untuk kondisi minim cahaya =====
  // Menaikkan gain (sensitivitas) dan exposure supaya foto lebih terang
  // di ruangan gelap, mirip menaikkan ISO di kamera HP.
  sensor_t *sensor = esp_camera_sensor_get();
  if (sensor != NULL) {
    sensor->set_brightness(sensor, 1);        // -2 s/d 2, naikkan sedikit
    sensor->set_contrast(sensor, 0);          // -2 s/d 2
    sensor->set_gainceiling(sensor, GAINCEILING_128X); // batas gain maksimal dinaikkan (default 2X)
    sensor->set_aec2(sensor, 1);              // aktifkan advanced auto-exposure control
    sensor->set_ae_level(sensor, 1);          // -2 s/d 2, geser auto-exposure ke arah lebih terang
    sensor->set_gain_ctrl(sensor, 1);         // pastikan auto gain control aktif
    sensor->set_exposure_ctrl(sensor, 1);     // pastikan auto exposure control aktif
  }

  return true;
}

void connectWiFi() {
  Serial.printf("Menghubungkan ke WiFi: %s\n", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  while (WiFi.status() != WL_CONNECTED) {
    delay(300);
    Serial.print(".");
  }
  Serial.println("\nWiFi terhubung, IP: " + WiFi.localIP().toString());
}

void captureAndVerify() {
  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) {
    Serial.println("Gagal mengambil foto");
    return;
  }

  Serial.println("Mengirim foto ke server untuk verifikasi...");

  HTTPClient http;
  http.begin(SERVER_URL);
  http.addHeader("Content-Type", "image/jpeg");

  int httpResponseCode = http.POST(fb->buf, fb->len);
  esp_camera_fb_return(fb); // kembalikan buffer kamera setelah dipakai

  if (httpResponseCode == 200) {
    String response = http.getString();
    Serial.println("Respons server: " + response);

    StaticJsonDocument<256> doc;
    DeserializationError error = deserializeJson(doc, response);

    if (!error) {
      bool access = doc["access"];
      const char* name = doc["name"] | "Tidak dikenali";

      if (access) {
        Serial.printf("AKSES DIBUKA untuk: %s\n", name);
        setRelay(true);
        delay(UNLOCK_DURATION_MS);
        setRelay(false);
      } else {
        Serial.println("AKSES DITOLAK - wajah tidak dikenali");
      }
    } else {
      Serial.println("Gagal parsing respons JSON dari server");
    }
  } else {
    Serial.printf("HTTP POST gagal, kode: %d\n", httpResponseCode);
  }

  http.end();
}

void setup() {
  Serial.begin(115200);
  Serial.setDebugOutput(false);

  pinMode(GPIO_RELAY, OUTPUT);
  setRelay(false); // pastikan pintu terkunci saat start

  pinMode(GPIO_FLASH_LED, OUTPUT);
  digitalWrite(GPIO_FLASH_LED, HIGH); // flash menyala terus selama sistem hidup

  if (!initCamera()) {
    Serial.println("Restart karena kamera gagal diinisialisasi...");
    delay(3000);
    ESP.restart();
  }

  connectWiFi();
  Serial.printf("Sistem siap. Otomatis mengambil foto tiap %d ms.\n", CAPTURE_INTERVAL_MS);
}

void loop() {
  // Jaga koneksi WiFi
  if (WiFi.status() != WL_CONNECTED) {
    connectWiFi();
  }

  unsigned long now = millis();

  if (now - lastCaptureTime > CAPTURE_INTERVAL_MS) {
    lastCaptureTime = now;
    captureAndVerify();
  }
}
