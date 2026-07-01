"""ElevenLabs text-to-speech service with simple in-memory audio cache."""

import uuid
import time
import threading
from typing import Optional
from elevenlabs.client import ElevenLabs
from app.config import settings

# In-memory cache: { audio_id: (audio_bytes, created_at_epoch) }
# Keeps audio for ~5 min, plenty of time for Twilio to fetch + play.
_audio_cache: dict[str, tuple[bytes, float]] = {}
_cache_lock = threading.Lock()
CACHE_TTL_SECONDS = 300  # 5 min

_client: Optional[ElevenLabs] = None


def _get_client() -> ElevenLabs:
    """Lazily initialize the ElevenLabs client."""
    global _client
    if _client is None:
        _client = ElevenLabs(api_key=settings.elevenlabs_api_key)
    return _client


def _evict_expired():
    """Drop expired entries from the cache; cheap to call on every set."""
    now = time.time()
    expired_keys = [k for k, (_, ts) in _audio_cache.items() if now - ts > CACHE_TTL_SECONDS]
    for k in expired_keys:
        _audio_cache.pop(k, None)


def generate_audio(text: str) -> Optional[str]:
    """Generate speech for given text. Returns audio_id, or None on failure."""
    if not settings.use_elevenlabs or not settings.elevenlabs_api_key:
        return None

    try:
        client = _get_client()
        # convert() returns a generator of audio chunks; concat to one bytes blob.
        audio_iter = client.text_to_speech.convert(
            voice_id=settings.elevenlabs_voice_id,
            text=text,
            model_id=settings.elevenlabs_model,
            output_format="mp3_22050_32",  # phone-quality mp3, smaller files
            optimize_streaming_latency = 4
        )
        audio_bytes = b"".join(audio_iter)

        audio_id = uuid.uuid4().hex
        with _cache_lock:
            _evict_expired()
            _audio_cache[audio_id] = (audio_bytes, time.time())

        print(f"ELEVENLABS GENERATED audio_id={audio_id} bytes={len(audio_bytes)}")
        return audio_id

    except Exception as e:
        print("ELEVENLABS ERROR:", str(e))
        return None


def get_audio(audio_id: str) -> Optional[bytes]:
    """Retrieve cached audio bytes by id, or None if expired/missing."""
    with _cache_lock:
        entry = _audio_cache.get(audio_id)
        if not entry:
            return None
        audio_bytes, ts = entry
        if time.time() - ts > CACHE_TTL_SECONDS:
            _audio_cache.pop(audio_id, None)
            return None
        return audio_bytes