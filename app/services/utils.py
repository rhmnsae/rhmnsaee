
import os
import time
import threading
import datetime
import logging
from functools import wraps
import random
import shutil
from flask import abort, jsonify, request, current_app, g

# Dictionary untuk menyimpan status lock per user
_user_locks = {}
_global_lock = threading.Lock()

# Setup logging
logger = logging.getLogger(__name__)

class UserLock:
    """
    Implementasi lock berbasis file untuk mencegah proses duplikasi
    """
    def __init__(self, user_id):
        self.user_id = user_id
        self.lock_time = datetime.datetime.now()
        self.lock_file = None
        
        # Buat direktori untuk lock file jika belum ada
        self.lock_dir = os.path.join(os.getcwd(), 'locks')
        os.makedirs(self.lock_dir, exist_ok=True)
        
        # Gunakan file sebagai indikator lock
        self.lock_file = os.path.join(self.lock_dir, f'user_{user_id}.lock')
    
    def acquire(self):
        """Mendapatkan lock"""
        try:
            # Cek apakah file lock sudah ada
            if os.path.exists(self.lock_file):
                # Cek apakah lock sudah kadaluarsa (lebih dari 5 menit)
                file_mtime = os.path.getmtime(self.lock_file)
                current_time = time.time()
                
                # Jika lock lebih dari 5 menit, anggap sudah kadaluarsa
                if current_time - file_mtime > 300:  # 5 menit
                    try:
                        os.remove(self.lock_file)
                        logger.info(f"Lock kadaluarsa untuk user {self.user_id} dihapus")
                    except Exception as e:
                        logger.error(f"Gagal menghapus lock kadaluarsa: {e}")
                        return False
                else:
                    logger.warning(f"User {self.user_id} memiliki lock aktif, tidak bisa mendapatkan lock baru")
                    return False
            
            # Buat file lock baru
            with open(self.lock_file, 'w') as f:
                f.write(f"Lock created at {self.lock_time}")
                
            logger.info(f"Lock berhasil dibuat untuk user {self.user_id}")
            return True
        except Exception as e:
            logger.error(f"Error saat acquire lock: {e}")
            return False
    
    def release(self):
        """Melepaskan lock"""
        try:
            if self.lock_file and os.path.exists(self.lock_file):
                os.remove(self.lock_file)
                logger.info(f"Lock berhasil dilepaskan untuk user {self.user_id}")
            else:
                logger.warning(f"Tidak ada lock file untuk dilepaskan untuk user {self.user_id}")
        except Exception as e:
            logger.error(f"Error saat release lock: {e}")
    
    def force_cleanup(self):
        """Paksa membersihkan lock apapun yang mungkin tersisa"""
        try:
            if os.path.exists(self.lock_file):
                os.remove(self.lock_file)
                logger.info(f"Lock untuk user {self.user_id} dibersihkan secara paksa")
            return True
        except Exception as e:
            logger.error(f"Gagal membersihkan lock secara paksa: {e}")
            return False

def cleanup_all_locks():
    """Fungsi untuk membersihkan semua lock yang ada"""
    try:
        lock_dir = os.path.join(os.getcwd(), 'locks')
        if os.path.exists(lock_dir):
            # Hapus folder locks dan buat ulang
            shutil.rmtree(lock_dir)
            os.makedirs(lock_dir, exist_ok=True)
            logger.info("Semua lock berhasil dibersihkan")
            
            # Reset user_locks dictionary
            with _global_lock:
                _user_locks.clear()
        return True
    except Exception as e:
        logger.error(f"Gagal membersihkan semua lock: {e}")
        return False

def acquire_user_lock(user_id, timeout=30):
    """
    Mencoba mendapatkan lock untuk user tertentu dengan retry
    
    Args:
        user_id: ID user yang ingin mendapatkan lock
        timeout: Waktu maksimal tunggu dalam detik
    
    Returns:
        True jika berhasil mendapatkan lock, False jika gagal
    """
    with _global_lock:
        # Cek apakah user sudah memegang lock
        if user_id in _user_locks:
            # Cek apakah lock sudah kadaluarsa (lebih dari 5 menit)
            lock_obj = _user_locks[user_id]
            current_time = datetime.datetime.now()
            time_diff = (current_time - lock_obj.lock_time).total_seconds()
            
            # Jika lock lebih dari 5 menit, lepaskan dan buat baru
            if time_diff > 300:  # 5 menit
                logger.info(f"Lock untuk user {user_id} sudah kadaluarsa, melepaskan lock")
                release_user_lock(user_id)
            else:
                logger.warning(f"User {user_id} sudah memiliki lock aktif")
                return False
        
        # Buat lock baru
        lock_obj = UserLock(user_id)
        
        # Coba dapatkan lock dengan retry exponential backoff
        start_time = time.time()
        retry_count = 0
        max_retries = 5
        
        while time.time() - start_time < timeout and retry_count < max_retries:
            if lock_obj.acquire():
                _user_locks[user_id] = lock_obj
                return True
            
            # Exponential backoff dengan jitter untuk menghindari thundering herd
            retry_count += 1
            wait_time = min(2 ** retry_count + random.uniform(0, 1), timeout / 2)
            logger.info(f"Mencoba acquire lock lagi dalam {wait_time:.2f} detik (retry {retry_count}/{max_retries})")
            time.sleep(wait_time)
        
        logger.error(f"Gagal mendapatkan lock setelah {retry_count} percobaan")
        return False

def release_user_lock(user_id):
    """
    Melepaskan lock untuk user tertentu
    
    Args:
        user_id: ID user yang ingin melepaskan lock
    """
    with _global_lock:
        if user_id in _user_locks:
            _user_locks[user_id].release()
            del _user_locks[user_id]
            logger.info(f"Lock untuk user {user_id} berhasil dilepaskan")
        else:
            # Coba bersihkan file lock jika ada, meskipun tidak ada entry di dictionary
            lock_obj = UserLock(user_id)
            lock_obj.force_cleanup()
            logger.warning(f"Tidak ada lock di memory untuk user {user_id}, mencoba bersihkan file lock")

def check_force_cleanup_parameter(user_id):
    """Fungsi untuk mengecek parameter force_cleanup di URL"""
    try:
        # Jika ada parameter force_cleanup, bersihkan lock sebelum mencoba
        force_cleanup = request.args.get('force_cleanup', 'false').lower() == 'true'
        if force_cleanup:
            lock_obj = UserLock(user_id)
            lock_obj.force_cleanup()
            logger.info(f"Lock untuk user {user_id} dibersihkan secara paksa dari URL parameter")
            return True
        return False
    except:
        return False

# Decorator berbasis class untuk lebih banyak kontrol
class UserLockRequired:
    """Decorator berbasis class yang lebih kuat untuk mengontrol lock per user"""
    
    def __init__(self, f):
        self.f = f
        wraps(f)(self)
    
    def __call__(self, *args, **kwargs):
        from flask_login import current_user
        
        if not current_user.is_authenticated:
            abort(401, description="User tidak terautentikasi")
        
        user_id = current_user.id
        
        # Bersihkan lock jika diminta
        check_force_cleanup_parameter(user_id)
        
        # Coba dapatkan lock
        if not acquire_user_lock(user_id):
            # Simpan informasi error ke variabel global untuk diakses di tempat lain
            g.lock_error = True
            g.lock_user_id = user_id
            
            # Gunakan Flask abort untuk benar-benar menghentikan eksekusi function
            response = jsonify({
                'error': 'Proses sebelumnya masih berjalan. Harap tunggu beberapa saat atau refresh halaman.',
                'code': 'LOCK_EXISTS'
            })
            response.status_code = 429
            abort(response)
        
        try:
            # Jalankan fungsi yang dibungkus
            return self.f(*args, **kwargs)
        except Exception as e:
            current_app.logger.error(f"Error saat menjalankan fungsi: {e}")
            raise
        finally:
            # Pastikan lock selalu dilepaskan setelah selesai
            release_user_lock(user_id)
            current_app.logger.info(f"Lock untuk user {user_id} dilepaskan setelah fungsi selesai")

# Backward compatibility dengan decorator style lama
def user_lock_required(f):
    return UserLockRequired(f)