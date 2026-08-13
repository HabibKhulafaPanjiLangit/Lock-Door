"""
Sistem Keamanan Pintu berbasis ESP32-CAM + Face Recognition
============================================================
Backend Flask ini punya 2 tugas utama:
1. Registrasi wajah baru (lewat dashboard web) -> disimpan sebagai face encoding di SQLite
2. Verifikasi wajah yang dikirim ESP32-CAM -> dibandingkan dengan encoding yang terdaftar

Endpoint penting:
- GET  /                 -> dashboard (daftar wajah terdaftar + log akses)
- POST /register         -> daftarkan wajah baru (multipart form: name, image)
- POST /verify           -> dipanggil ESP32-CAM, body = raw JPEG bytes
- GET  /api/logs         -> data log akses (JSON, dipakai dashboard)
- GET  /api/registered   -> data wajah terdaftar (JSON)
- POST /api/registered/<id>/delete -> hapus wajah terdaftar
"""

import os
import io
import json
import sqlite3
import datetime

import numpy as np
import cv2
import face_recognition
from PIL import Image
from flask import Flask, request, jsonify, render_template, send_from_directory

try:
    import pillow_heif
    pillow_heif.register_heif_opener()  # supaya PIL bisa buka file .heic (foto iPhone)
except ImportError:
    pass  # kalau tidak terinstall, foto HEIC akan gagal dengan pesan error yang jelas

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "door_security.db")
FACES_DIR = os.path.join(BASE_DIR, "static", "faces")
LOGS_DIR = os.path.join(BASE_DIR, "static", "logs")

# Ambang batas kecocokan wajah. Semakin kecil semakin ketat.
# 0.6 adalah nilai default yang direkomendasikan library face_recognition.
MATCH_THRESHOLD = 0.5

# Model deteksi wajah: "hog" (cepat, cocok untuk CPU biasa tanpa GPU)
# alternatifnya "cnn" (lebih akurat tapi jauh lebih lambat tanpa GPU)
FACE_DETECTION_MODEL = "hog"

app = Flask(__name__)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS registered_faces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            encoding TEXT NOT NULL,
            photo_path TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS access_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            matched INTEGER NOT NULL,
            confidence REAL,
            photo_path TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


MAX_IMAGE_DIMENSION = 800  # resize foto besar supaya proses deteksi wajah lebih cepat


def resize_image_bytes(image_bytes):
    """
    Resize foto supaya sisi terpanjangnya maksimal MAX_IMAGE_DIMENSION px.
    Foto dari kamera HP (misal 3000x4000px) bisa membuat face_recognition
    sangat lambat / seperti macet kalau tidak di-resize dulu.
    """
    image = Image.open(io.BytesIO(image_bytes))
    image = image.convert("RGB")  # jaga-jaga kalau ada foto format lain (misal PNG dgn alpha)

    width, height = image.size
    if max(width, height) > MAX_IMAGE_DIMENSION:
        scale = MAX_IMAGE_DIMENSION / max(width, height)
        new_size = (int(width * scale), int(height * scale))
        image = image.resize(new_size, Image.LANCZOS)
        print(f"[INFO] Foto di-resize dari {width}x{height} ke {new_size[0]}x{new_size[1]}")

    output = io.BytesIO()
    image.save(output, format="JPEG", quality=90)
    return output.getvalue()


def enhance_low_light(rgb_image):
    """
    Perbaiki kontras foto yang diambil di kondisi minim cahaya, memakai CLAHE
    (Contrast Limited Adaptive Histogram Equalization) supaya detail wajah
    yang tenggelam dalam gelap jadi lebih kelihatan untuk proses deteksi.

    Hanya memproses kalau foto memang gelap (rata-rata brightness rendah),
    supaya foto yang sudah terang tidak diutak-atik / tidak jadi over-processed.
    """
    gray = cv2.cvtColor(rgb_image, cv2.COLOR_RGB2GRAY)
    avg_brightness = np.mean(gray)

    DARK_THRESHOLD = 90  # skala 0-255, di bawah ini dianggap "gelap"
    if avg_brightness >= DARK_THRESHOLD:
        return rgb_image  # foto sudah cukup terang, tidak perlu diproses

    print(f"[INFO] Foto terdeteksi gelap (brightness={avg_brightness:.1f}), menerapkan CLAHE...")

    # Konversi ke ruang warna LAB supaya perbaikan kontras hanya kena
    # channel "lightness", warna asli (a,b) tidak berubah
    lab = cv2.cvtColor(rgb_image, cv2.COLOR_RGB2LAB)
    l_channel, a_channel, b_channel = cv2.split(lab)

    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    l_enhanced = clahe.apply(l_channel)

    lab_enhanced = cv2.merge((l_enhanced, a_channel, b_channel))
    enhanced_rgb = cv2.cvtColor(lab_enhanced, cv2.COLOR_LAB2RGB)
    return enhanced_rgb


def load_known_faces():
    """Ambil semua encoding wajah terdaftar dari DB."""
    conn = get_db()
    rows = conn.execute("SELECT id, name, encoding FROM registered_faces").fetchall()
    conn.close()

    known_ids, known_names, known_encodings = [], [], []
    for row in rows:
        known_ids.append(row["id"])
        known_names.append(row["name"])
        known_encodings.append(np.array(json.loads(row["encoding"])))
    return known_ids, known_names, known_encodings


@app.route("/")
def dashboard():
    return render_template("index.html")


@app.route("/register", methods=["POST"])
def register():
    """
    Registrasi wajah baru.
    Form-data: name (string), image (file)
    """
    name = request.form.get("name", "").strip()
    image_file = request.files.get("image")

    if not name or not image_file:
        return jsonify({"success": False, "message": "Nama dan foto wajib diisi"}), 400

    print(f"[INFO] Menerima foto registrasi untuk: {name}")
    raw_bytes = image_file.read()

    try:
        image_bytes = resize_image_bytes(raw_bytes)
    except Exception as e:
        print(f"[ERROR] Gagal membuka foto: {e}")
        return jsonify({
            "success": False,
            "message": "Format foto tidak didukung. Gunakan foto JPG atau PNG (bukan HEIC/format lain)."
        }), 400

    print("[INFO] Mendeteksi wajah (model: HOG)...")
    image = face_recognition.load_image_file(io.BytesIO(image_bytes))
    image = enhance_low_light(image)
    face_locations = face_recognition.face_locations(image, model=FACE_DETECTION_MODEL)
    face_encodings = face_recognition.face_encodings(image, known_face_locations=face_locations)
    print(f"[INFO] Deteksi selesai, ditemukan {len(face_encodings)} wajah")

    if len(face_encodings) == 0:
        return jsonify({"success": False, "message": "Tidak ada wajah terdeteksi pada foto"}), 400
    if len(face_encodings) > 1:
        return jsonify({"success": False, "message": "Foto berisi lebih dari satu wajah, gunakan foto dengan 1 wajah saja"}), 400

    encoding = face_encodings[0]

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{timestamp}_{name.replace(' ', '_')}.jpg"
    filepath = os.path.join(FACES_DIR, filename)
    with open(filepath, "wb") as f:
        f.write(image_bytes)

    conn = get_db()
    conn.execute(
        "INSERT INTO registered_faces (name, encoding, photo_path, created_at) VALUES (?, ?, ?, ?)",
        (name, json.dumps(encoding.tolist()), f"faces/{filename}", datetime.datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()

    return jsonify({"success": True, "message": f"Wajah '{name}' berhasil didaftarkan"})


@app.route("/verify", methods=["POST"])
def verify():
    """
    Dipanggil oleh ESP32-CAM. Body request = raw bytes JPEG (Content-Type: image/jpeg).
    Response JSON:
        {
            "access": true/false,
            "name": "Nama Orang" atau null,
            "confidence": 0.83
        }
    """
    raw_bytes = request.get_data()
    if not raw_bytes:
        return jsonify({"access": False, "name": None, "message": "Tidak ada gambar diterima"}), 400

    print(f"[INFO] Menerima foto dari ESP32-CAM ({len(raw_bytes)} bytes)")

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_filename = f"{timestamp}.jpg"
    log_filepath = os.path.join(LOGS_DIR, log_filename)
    with open(log_filepath, "wb") as f:
        f.write(raw_bytes)

    try:
        image_bytes = resize_image_bytes(raw_bytes)
        print("[INFO] Mendeteksi wajah (model: HOG)...")
        image = face_recognition.load_image_file(io.BytesIO(image_bytes))
        image = enhance_low_light(image)
        face_locations = face_recognition.face_locations(image, model=FACE_DETECTION_MODEL)
        face_encodings = face_recognition.face_encodings(image, known_face_locations=face_locations)
        print(f"[INFO] Deteksi selesai, ditemukan {len(face_encodings)} wajah")
    except Exception as e:
        print(f"[ERROR] {e}")
        return jsonify({"access": False, "name": None, "message": f"Gagal memproses gambar: {e}"}), 400

    access_granted = False
    matched_name = None
    confidence = 0.0

    if len(face_encodings) > 0:
        known_ids, known_names, known_encodings = load_known_faces()

        if known_encodings:
            input_encoding = face_encodings[0]
            distances = face_recognition.face_distance(known_encodings, input_encoding)
            best_idx = int(np.argmin(distances))
            best_distance = distances[best_idx]

            if best_distance <= MATCH_THRESHOLD:
                access_granted = True
                matched_name = known_names[best_idx]
                confidence = round(1 - best_distance, 3)

    conn = get_db()
    conn.execute(
        "INSERT INTO access_logs (name, matched, confidence, photo_path, created_at) VALUES (?, ?, ?, ?, ?)",
        (matched_name, int(access_granted), confidence, f"logs/{log_filename}", datetime.datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()

    return jsonify({
        "access": access_granted,
        "name": matched_name,
        "confidence": confidence,
    })


@app.route("/api/logs")
def api_logs():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM access_logs ORDER BY id DESC LIMIT 100"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/registered")
def api_registered():
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name, photo_path, created_at FROM registered_faces ORDER BY id DESC"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/registered/<int:face_id>/delete", methods=["POST"])
def delete_registered(face_id):
    conn = get_db()
    conn.execute("DELETE FROM registered_faces WHERE id = ?", (face_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


if __name__ == "__main__":
    os.makedirs(FACES_DIR, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)
    init_db()
    # host 0.0.0.0 supaya bisa diakses dari ESP32-CAM di jaringan yang sama
    app.run(host="0.0.0.0", port=5000, debug=True)
