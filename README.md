# Sistem Keamanan Pintu — ESP32-CAM + Face Recognition

Arsitektur:

```
[ESP32-CAM] --foto tiap 2 detik--> [Server Flask]
     |                                    |
     |                              face_recognition
     |                              cocokkan wajah
     |                                    |
[Relay+Solenoid] <----respon JSON--------+
```

1. ESP32-CAM otomatis mengambil foto setiap beberapa detik (default 2 detik, bisa diubah) dan mengirimkannya (raw JPEG) ke server Flask via HTTP POST ke `/verify`.
2. Server mendeteksi wajah pada foto dan membandingkannya dengan wajah-wajah yang sudah didaftarkan (disimpan sebagai *face encoding* di SQLite).
3. Server membalas JSON: `{"access": true/false, "name": "...", "confidence": 0.83}`.
4. Jika `access: true`, ESP32-CAM mengaktifkan relay yang terhubung ke solenoid door lock/EMS selama beberapa detik.

---

## 1. Setup Backend (Server)

```bash
cd backend
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# Ubuntu/Debian: install dependency sistem dulu untuk dlib
sudo apt update && sudo apt install -y cmake build-essential

pip install -r requirements.txt
python app.py
```

Server akan berjalan di `http://0.0.0.0:5000`. Buka `http://<IP_KOMPUTER_ANDA>:5000` dari browser untuk:
- Mendaftarkan wajah baru (upload foto + nama)
- Melihat log akses secara real-time

> Cari IP komputer server dengan `ipconfig` (Windows) atau `ip addr` (Linux/Mac). Pastikan ESP32-CAM dan komputer server berada di jaringan WiFi yang sama.

## 2. Setup ESP32-CAM (Firmware)

1. Buka `esp32cam/esp32cam_door_security.ino` di Arduino IDE.
2. Install board package **ESP32** (via Board Manager) dan library **ArduinoJson** (via Library Manager).
3. Pilih board **AI Thinker ESP32-CAM**.
4. Edit bagian konfigurasi di atas file:
   ```cpp
   const char* WIFI_SSID     = "NAMA_WIFI_ANDA";
   const char* WIFI_PASSWORD = "PASSWORD_WIFI_ANDA";
   const char* SERVER_URL    = "http://192.168.1.100:5000/verify"; // ganti dengan IP server Anda
   ```
5. Upload ke ESP32-CAM (perlu FTDI programmer / USB-to-serial adapter, dan tekan tombol **IO0 + RESET** untuk masuk mode flashing).

## 3. Wiring (Perkabelan)

| Komponen              | Pin ESP32-CAM        | Keterangan                                   |
|------------------------|----------------------|-----------------------------------------------|
| Relay Module IN        | GPIO 13               | Kontrol ON/OFF ke solenoid lock               |
| Relay VCC              | 5V                    |                                                |
| Relay GND              | GND                   |                                                |
| Solenoid Lock          | Terminal relay (NO/COM)| Solenoid diberi daya terpisah (12V, lihat spek lock)|

**Catatan penting:**
- Solenoid door lock biasanya butuh 12V/DC dengan arus cukup besar — **jangan** disuplai langsung dari ESP32-CAM. Gunakan power supply 12V terpisah, dan relay hanya sebagai saklar.
- Gunakan relay module yang sudah ada opto-coupler supaya ESP32-CAM aman dari induksi listrik solenoid.
- ESP32-CAM sekarang tidak butuh sensor PIR/tombol — dia otomatis ambil foto & kirim ke server setiap `CAPTURE_INTERVAL_MS` (default 2000ms/2 detik). Ubah nilai ini di file `.ino` kalau mau lebih jarang/lebih sering.
- Pin 13 dipilih karena tidak dipakai kamera dan kita tidak menggunakan SD card.

## 4. Cara Pakai

1. Jalankan server (`python app.py`).
2. Buka dashboard di browser, daftarkan wajah pemilik rumah/karyawan (upload 1 foto wajah yang jelas, cahaya cukup, satu wajah per foto).
3. Nyalakan ESP32-CAM. Dia akan otomatis memfoto dan mengirim ke server setiap 2 detik (atau sesuai `CAPTURE_INTERVAL_MS` yang diatur).
4. Jika wajah cocok (confidence di atas threshold), relay aktif dan solenoid membuka pintu selama 5 detik (bisa diubah di `UNLOCK_DURATION_MS`).
5. Semua percobaan akses (berhasil maupun gagal) tercatat di dashboard beserta fotonya.

## 5. Catatan Keamanan (Penting!)

Sistem face recognition berbasis foto tunggal seperti ini **rentan terhadap spoofing** — misalnya seseorang menunjukkan foto cetak/layar HP berisi wajah orang yang terdaftar. Untuk penggunaan produksi/serius, pertimbangkan tambahan:
- **Liveness detection** (deteksi kedipan mata, gerakan kepala, atau sensor kedalaman) supaya tidak bisa ditipu foto.
- **Multi-factor**: kombinasikan dengan PIN/RFID sebagai lapisan kedua.
- **HTTPS** antara ESP32-CAM dan server jika jaringan tidak sepenuhnya tepercaya (saat ini contoh memakai HTTP polos untuk kesederhanaan).
- Batasi siapa yang bisa mengakses dashboard registrasi (tambahkan login/password), karena siapa pun yang bisa mendaftarkan wajah baru otomatis bisa membuka pintu.

## 6. Struktur Folder

```
security-door/
├── backend/
│   ├── app.py                # Server Flask
│   ├── requirements.txt
│   ├── templates/index.html  # Dashboard
│   └── static/
│       ├── faces/            # Foto wajah terdaftar
│       └── logs/             # Foto setiap percobaan akses
└── esp32cam/
    └── esp32cam_door_security.ino
```
