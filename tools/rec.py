"""录系统正在放的声音（WASAPI 环回），存成 wav。

依赖：pyaudiowpatch（pip install pyaudiowpatch）
"""
import time
import wave

import numpy as np
import pyaudiowpatch as pa


def default_loopback(p):
    try:
        wasapi = p.get_host_api_info_by_type(pa.paWASAPI)
        spk = p.get_device_info_by_index(wasapi['defaultOutputDevice'])
        if spk.get('isLoopbackDevice'):
            return spk
        for lb in p.get_loopback_device_info_generator():
            if spk['name'] in lb['name']:
                return lb
    except Exception:
        pass
    for lb in p.get_loopback_device_info_generator():
        return lb
    return None


def record(seconds=15, out='out.wav'):
    p = pa.PyAudio()
    try:
        dev = default_loopback(p)
        if dev is None:
            raise RuntimeError('没有可用的环回设备（系统可能没有正在输出的声音设备）')
        rate = int(dev['defaultSampleRate'])
        ch = int(dev['maxInputChannels']) or 2
        stream = p.open(format=pa.paInt16, channels=ch, rate=rate,
                        input=True, input_device_index=dev['index'],
                        frames_per_buffer=1024)
        frames = []
        need = int(rate / 1024 * seconds)
        # ⚠️ 暂停/没有声音时 stream.read 有可能一直不返回，必须有硬性截止时间，
        #    否则整个调用就卡死（2026-10-28 实测过：暂停时 --hear 会挂住）
        deadline = time.time() + seconds * 2 + 5
        for _ in range(need):
            if time.time() > deadline:
                break
            frames.append(stream.read(1024, exception_on_overflow=False))
        stream.stop_stream()
        stream.close()
        data = b''.join(frames)
        if not data:
            raise RuntimeError('一秒都没录到（设备没出声或被占用）')

        with wave.open(out, 'wb') as w:
            w.setnchannels(ch)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(data)

        arr = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768
        return {'path': out, 'seconds': round(len(data) / (rate * ch * 2), 2),
                'rate': rate, 'channels': ch,
                'rms': float(np.sqrt((arr ** 2).mean()))}
    finally:
        p.terminate()
