"""Local Fish Audio Drama 3 inference through audio.cpp."""

import asyncio
import base64
import os
import re
import socket
import subprocess
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock, Timer
from time import monotonic

import httpx
from .audio_cache import reusable_voice_clip
from .config import ROOT, atomic_json, resolve
from .media import check_cancel, extract_audio, run, spawn, stop_process, wav_duration
from .model_backends import cancellable


DIRECTOR_TAGS = {
    'happy': 'brightly', 'angry': 'angrily', 'sad': 'sadly', 'afraid': 'fearfully',
    'disgusted': 'with disgust', 'melancholic': 'in a subdued voice',
    'surprised': 'with surprise', 'calm': 'calmly',
}

MODEL_NAME = 'drama-3-backbone-8bit-experts-Q2_K.gguf'
MODEL_BYTES = 17053647360
KANA = re.compile(r'[\u3041-\u309f\u30a1-\u30fa\uff66-\uff9f]')
MANDARIN_DIRECTION = '用标准普通话说下面的中文台词，保留参考音的音色与表演'


def paths(settings):
    exe_name = 'audiocpp_cli.exe' if os.name == 'nt' else 'audiocpp_cli'
    return (
        resolve(settings.drama3_executable) if settings.drama3_executable else ROOT / 'runtime' / 'drama3' / exe_name,
        resolve(settings.drama3_model) if settings.drama3_model else ROOT / 'runtime' / 'drama3' / MODEL_NAME,
    )


def status(settings):
    executable, model = paths(settings)
    if not executable.is_file():
        return '请先安装 Drama 3 的 audio.cpp 推理工具'
    try:
        if (not settings.drama3_model and model.stat().st_size != MODEL_BYTES):
            return 'Drama 3 权重尚未下载完成'
        with model.open('rb') as stream:
            if stream.read(4) != b'GGUF':
                return 'Drama 3 需要有效的 GGUF 权重文件'
    except OSError:
        return 'Drama 3 权重尚未下载完成'
    return ''


def server_path(settings):
    cli, _ = paths(settings)
    return cli.with_name('audiocpp_server' + cli.suffix)


class JapaneseOutputCheck:
    """Screen risky cross-language takes locally without taking the GPU."""

    def __init__(self, model_path):
        self.path = model_path
        self.model = None

    def reject(self, audio, settings, log, cancel):
        import numpy as np
        from faster_whisper import WhisperModel

        check_cancel(cancel)
        if self.model is None:
            self.model = WhisperModel(str(self.path), device='cpu', compute_type='int8',
                                      cpu_threads=4, local_files_only=True)
        with TemporaryDirectory(prefix='drama3-language-', dir=audio.parent) as temp:
            pcm = Path(temp) / 'speech.wav'
            extract_audio(audio, pcm, settings, log, cancel, sample_rate=16000)
            with wave.open(str(pcm), 'rb') as stream:
                samples = np.frombuffer(stream.readframes(stream.getnframes()), dtype='<i2').astype('float32') / 32768
        parts, info = self.model.transcribe(samples, language=None, beam_size=3, condition_on_previous_text=False)
        text = ''.join(part.text for part in parts)
        check_cancel(cancel)
        kana_count = len(KANA.findall(text))
        return kana_count >= 4 or (kana_count >= 2 and info.language == 'ja' and info.language_probability >= .65)


class Drama3Session:
    """Reuse the local audio.cpp server until the GPU is needed elsewhere."""

    def __init__(self):
        self.lock = RLock()
        self.process = self.scratch = self.key = self.timer = None
        self.address = ''
        self.deadline = 0
        self.loaded = False
        self.closed = False

    def identity(self, settings):
        executable, model = server_path(settings), paths(settings)[1]
        files = tuple((path, path.stat().st_mtime_ns, path.stat().st_size) for path in (executable, model))
        return files, settings.drama3_backend, settings.drama3_cpu_expert_layers

    def warm(self, settings):
        process = self.process
        if not self.loaded or not process or process.poll() is not None:
            return False
        if self.deadline and monotonic() >= self.deadline:
            return False
        try:
            return self.identity(settings) == self.key
        except OSError:
            return False

    def release(self):
        with self.lock:
            if self.timer:
                self.timer.cancel()
                self.timer = None
            if self.process:
                stop_process(self.process)
                self.process = None
            if self.scratch:
                self.scratch.cleanup()
                self.scratch = None
            self.key = None
            self.address = ''
            self.loaded = False
            self.deadline = 0

    def close(self):
        with self.lock:
            self.closed = True
            self.release()

    def _expire(self, deadline):
        with self.lock:
            if deadline == self.deadline:
                self.release()

    def retain(self, seconds):
        with self.lock:
            if not self.loaded:
                return
            if self.timer:
                self.timer.cancel()
            if seconds <= 0:
                self.release()
                return
            self.deadline = monotonic() + seconds
            self.timer = Timer(seconds, self._expire, args=(self.deadline,))
            self.timer.daemon = True
            self.timer.start()

    def _start(self, settings, key, log, cancel):
        self.release()
        executable, model = server_path(settings), paths(settings)[1]
        backend = settings.drama3_backend
        options = {'fish_drama3.context_length': 2048}
        if backend != 'cpu':
            options['moe_cpu_expert_layers'] = settings.drama3_cpu_expert_layers
        if backend == 'vulkan':
            options.update({'fish_drama3.codec_encoder_f32_accumulation': True,
                            'fish_drama3.codec_encoder_chunk_samples': 65536})
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        self.scratch = TemporaryDirectory(prefix='server-', dir=log.parent)
        config = Path(self.scratch.name) / 'server.json'
        atomic_json(config, dict(host='127.0.0.1', port=port, backend=backend, threads=8, ui=False,
            lazy_load=True, max_loaded_models=1, idle_unload_ms=max(300, settings.drama3_idle_seconds) * 1000,
            models=[dict(id='drama3', family='fish_drama3', path=str(model), task='tts',
                         mode='offline', session_options=options)]))
        self.address = f'http://127.0.0.1:{port}'
        with log.open('ab') as stream:
            stream.write(b'\n--- Drama 3 session start ---\n')
            stream.flush()
            self.process = spawn([executable, '--config', config], cwd=executable.parent,
                                 stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT)
        with httpx.Client(trust_env=False, timeout=.35) as client:
            for _ in range(60):
                check_cancel(cancel)
                if self.process.poll() is not None:
                    break
                try:
                    response = client.get(self.address + '/health')
                    if response.is_success and self.process.poll() is None:
                        self.key = key
                        return
                except httpx.TransportError:
                    pass
                cancel.wait(.10)
        raise RuntimeError('Drama 3 保温服务启动失败，请检查项目处理日志')

    def render(self, settings, text, reference, reference_text, seed, output, log, cancel):
        if not server_path(settings).is_file():
            self.release()
            return generate(settings, text, reference, reference_text, seed, output, log, cancel)
        with self.lock:
            check_cancel(cancel)
            if self.closed:
                raise RuntimeError('Drama 3 配音会话已关闭')
            if self.timer:
                self.timer.cancel()
                self.timer = None
            self.deadline = 0
            try:
                key = self.identity(settings)
                if self.key != key or not self.process or self.process.poll() is not None:
                    self._start(settings, key, log, cancel)
                payload = dict(model='drama3', input=text, seed=seed, max_tokens=500, response_format='wav')
                if reference:
                    payload.update(voice_ref=dict(type='base64', data=base64.b64encode(reference.read_bytes()).decode('ascii')),
                                   reference_text=reference_text)

                async def request():
                    async with httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(1800, connect=3)) as client:
                        return await client.post(self.address + '/v1/audio/speech', json=payload)

                try:
                    response = asyncio.run(cancellable(request(), cancel))
                except httpx.TransportError as exc:
                    raise RuntimeError('Drama 3 保温进程通信失败，请检查项目处理日志') from exc
                if not response.is_success:
                    raise RuntimeError(f'Drama 3 HTTP {response.status_code}：{response.text[:500]}')
                with TemporaryDirectory(prefix='drama3-', dir=output.parent) as temp:
                    rendered = Path(temp) / 'speech.wav'
                    rendered.write_bytes(response.content)
                    save_audio(rendered, output, settings, log, cancel)
                self.loaded = True
            except Exception:
                self.release()
                raise


def save_audio(rendered, output, settings, log, cancel):
    check_cancel(cancel)
    normalized = rendered.with_name('normalized.wav')
    extract_audio(rendered, normalized, settings, log, cancel, sample_rate=44100)
    if not reusable_voice_clip(normalized):
        raise ValueError('Drama 3 未生成有效音频')
    check_cancel(cancel)
    normalized.replace(output)


def generate(settings, text, reference, reference_text, seed, output, log, cancel):
    executable, model = paths(settings)
    backend = settings.drama3_backend
    with TemporaryDirectory(prefix='drama3-', dir=output.parent) as temp:
        rendered = Path(temp) / 'speech.wav'
        command = [
            executable, '--task', 'clon' if reference else 'tts', '--family', 'fish_drama3',
            '--model', model, '--backend', backend, '--mode', 'offline',
            '--session-option', 'fish_drama3.context_length=2048',
            '--max-tokens', '500', '--seed', str(seed), '--threads', '8',
            '--text', text, '--out', rendered, '--metrics',
        ]
        if backend != 'cpu':
            command += ['--session-option', f'moe_cpu_expert_layers={settings.drama3_cpu_expert_layers}']
        if reference:
            command += ['--voice-ref', reference, '--request-option', f'reference_text={reference_text}']
            if backend == 'vulkan':
                command += ['--session-option', 'fish_drama3.codec_encoder_f32_accumulation=true']
                if wav_duration(reference) > 12:
                    command += ['--session-option', 'fish_drama3.codec_encoder_chunk_samples=65536']
        run(command, log, cancel, cwd=executable.parent, timeout=1800)
        save_audio(rendered, output, settings, log, cancel)
