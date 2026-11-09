# PawRoyalty Backend — siap diunggah ke GitHub untuk Render

Ini adalah BACKEND SAJA, bukan APK. Source dari PawRoyalty Studio v1.4.1.
Tidak ada token API, project credential, atau service-account pribadi dalam paket ini.

## A. Buat repository GitHub
1. Masuk https://github.com/new dan buat repository baru `pawroyalty-backend` (Private direkomendasikan).
2. Klik **Add file > Upload files** dari repository baru tersebut.
3. EKSTRAK file ZIP ini terlebih dahulu. Upload ISI folder `PawRoyalty_Render_GitHub_Ready` (app.py, requirements.txt, render.yaml, dll), bukan ZIP-nya.
4. Commit perubahan ke branch `main`.

## B. Deploy ke Render
1. Di Render Dashboard pilih **New > Web Service** lalu **Git Provider > GitHub**.
2. Hubungkan GitHub; pilih repository `pawroyalty-backend`; klik **Connect**.
3. Gunakan setting berikut (manual Web Service):
   Name: `pawroyalty-api-demo`
   Language/Runtime: `Python 3`
   Branch: `main`
   Root Directory: (kosong, karena file app.py ada di root)
   Build Command: `pip install -r requirements.txt`
   Start Command: `uvicorn app:app --host 0.0.0.0 --port $PORT --no-proxy-headers`
   Instance type/plan: `Free`
   Health check path: `/health`
4. **SEBELUM membukanya ke pengguna umum**, set environment variables:
   APP_ENV=production
   REQUIRE_APP_CHECK=true
   IMAGE_PROVIDER=cloudflare
   GLOBAL_DAILY_LIMIT=0
   DAILY_CREDITS=5
   DAILY_IP_LIMIT=0
   SQLITE_PATH=/tmp/pawroyalty.db
   ENABLE_PAID_IMAGE_GENERATION=false
   ENABLE_FREE_GEMINI_CAPTIONS=false
5. Tambahkan `FIREBASE_PROJECT_ID`, `CF_ACCOUNT_ID`, `CF_API_TOKEN` sebagai Environment Variables/Secrets di dashboard Render. Jangan di GitHub.
6. Firebase service account: Render Service > Environment > **Secret Files** > buat `firebase-service-account.json` dan masukkan JSON yang diunduh dari Firebase Console; set `GOOGLE_APPLICATION_CREDENTIALS=/etc/secrets/firebase-service-account.json`. Jangan pernah upload file service-account ke GitHub atau chat.
7. Deploy, lalu buka `https://<nama-layanan>.onrender.com/health` untuk memeriksa apakah FastAPI online. Ini tidak membuktikan koneksi Firebase/Cloudflare berhasil.

Alternatif: jika memakai **New > Blueprint**, Render akan membaca `render.yaml` dan menyiapkan sebagian besar konfigurasi secara otomatis. Variabel `sync: false` akan diminta di dashboard; jangan berikan token melalui chat.

## Penting sebelum mengaktifkan AI
**Render Free punya filesystem sementara.** Semua data SQLite (kuota global/per-user, laporan, catatan generasi, permintaan penghapusan) bisa hilang ketika layanan tidur/restart/deploy. Oleh karena itu `GLOBAL_DAILY_LIMIT=0` sengaja menjaga AI nonaktif setelah deployment demo.

Sebelum mengubahnya menjadi nilai di atas nol, pindahkan database ke storage permanen (mis. Firestore atau Postgres dengan skema/driver yang sesuai) ATAU pakai Render plan berbayar dengan persistent disk dan tambahkan monitoring abuse. Backend sekarang menggunakan SQLite, jadi jangan asal mengganti SQLite_PATH menjadi URL PostgreSQL; itu butuh perubahan kode.

Sebelum rilis ke Play Store, konfigurasi Firebase Auth dan App Check, publikasi kebijakan privasi yang akurat, jalur penghapusan data dan moderasi laporan, uji aplikasi di perangkat Android, serta pastikan token Cloudflare lama yang sudah dibagikan telah dirotasi.

## Tes lokal

```
pip install -r requirements.txt pytest
python -m pytest -q
```

Pada versi v1.4.1, 33 pengujian mock berhasil. Ini tidak membuktikan model Cloudflare asli sukses menghasilkan gambar.
