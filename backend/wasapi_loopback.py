"""Small dependency-free WASAPI loopback reader for Windows.

This follows the same important design as NexQ: system audio is captured from
the render endpoint with AUDCLNT_STREAMFLAGS_LOOPBACK.  It does not depend on
the sound card exposing a fragile "Stereo Mix" input endpoint.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
import uuid
from ctypes import wintypes
from typing import Callable

import numpy as np


logger = logging.getLogger(__name__)

_HRESULT = ctypes.c_long
_VOID_P = ctypes.c_void_p
_DWORD = wintypes.DWORD
_UINT32 = ctypes.c_uint32
_REF_TIME = ctypes.c_longlong

_CLSCTX_ALL = 0x17
_COINIT_MULTITHREADED = 0x0
_E_RENDER = 0
_E_CONSOLE = 0
_AUDCLNT_SHAREMODE_SHARED = 0
_AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
_AUDCLNT_BUFFERFLAGS_SILENT = 0x2
_WAVE_FORMAT_PCM = 0x0001
_WAVE_FORMAT_IEEE_FLOAT = 0x0003
_WAVE_FORMAT_EXTENSIBLE = 0xFFFE

_CLSID_MM_DEVICE_ENUMERATOR = "BCDE0395-E52F-467C-8E3D-C4579291692E"
_IID_IMM_DEVICE_ENUMERATOR = "A95664D2-9614-4F35-A746-DE8DB63617E6"
_IID_IAUDIO_CLIENT = "1CB9AD4C-DBFA-4C32-B178-C2F568A703B2"
_IID_IAUDIO_CAPTURE_CLIENT = "C8ADBD64-E71E-48A0-A4DE-185C395CD317"
_SUBFORMAT_PCM = "00000001-0000-0010-8000-00AA00389B71"
_SUBFORMAT_FLOAT = "00000003-0000-0010-8000-00AA00389B71"


class _GUID(ctypes.Structure):
    _fields_ = [
        ("data1", wintypes.DWORD),
        ("data2", wintypes.WORD),
        ("data3", wintypes.WORD),
        ("data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_string(cls, value: str) -> "_GUID":
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


class _WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]


def _com_call(
    interface: _VOID_P,
    index: int,
    restype: object,
    argtypes: list[object],
    *args: object,
) -> object:
    """Call a COM vtable method without a third-party COM package."""
    vtable = ctypes.cast(interface, ctypes.POINTER(_VOID_P))[0]
    address = ctypes.cast(vtable, ctypes.POINTER(_VOID_P))[index]
    function = ctypes.WINFUNCTYPE(restype, _VOID_P, *argtypes)(address)
    return function(interface, *args)


def _check_hresult(result: int, operation: str) -> None:
    if result < 0:
        raise RuntimeError(f"{operation} failed (HRESULT 0x{result & 0xFFFFFFFF:08X})")


def _release(interface: _VOID_P | None) -> None:
    if interface:
        _com_call(interface, 2, _HRESULT, [])


class WasapiLoopback:
    """Capture the default Windows render endpoint as normalized float audio."""

    def capture(
        self,
        stop_event: threading.Event,
        on_audio: Callable[[np.ndarray, int], None],
    ) -> None:
        if not hasattr(ctypes, "windll"):
            raise RuntimeError("WASAPI loopback is only available on Windows")

        ole32 = ctypes.windll.ole32
        kernel32 = ctypes.windll.kernel32
        initialized = False
        enumerator = device = audio_client = capture_client = None
        mix_format = _VOID_P()

        try:
            result = ole32.CoInitializeEx(None, _COINIT_MULTITHREADED)
            if result in (0, 1):
                initialized = True
            elif result not in (0x80010106,):
                _check_hresult(result, "CoInitializeEx")

            clsid = _GUID.from_string(_CLSID_MM_DEVICE_ENUMERATOR)
            iid_enumerator = _GUID.from_string(_IID_IMM_DEVICE_ENUMERATOR)
            enumerator = _VOID_P()
            ole32.CoCreateInstance.argtypes = [
                ctypes.POINTER(_GUID),
                _VOID_P,
                _DWORD,
                ctypes.POINTER(_GUID),
                ctypes.POINTER(_VOID_P),
            ]
            ole32.CoCreateInstance.restype = _HRESULT
            _check_hresult(
                ole32.CoCreateInstance(
                    ctypes.byref(clsid),
                    None,
                    _CLSCTX_ALL,
                    ctypes.byref(iid_enumerator),
                    ctypes.byref(enumerator),
                ),
                "CoCreateInstance(MMDeviceEnumerator)",
            )

            device = _VOID_P()
            _check_hresult(
                _com_call(
                    enumerator,
                    4,
                    _HRESULT,
                    [_DWORD, _DWORD, ctypes.POINTER(_VOID_P)],
                    _E_RENDER,
                    _E_CONSOLE,
                    ctypes.byref(device),
                ),
                "GetDefaultAudioEndpoint",
            )

            audio_client = _VOID_P()
            iid_audio_client = _GUID.from_string(_IID_IAUDIO_CLIENT)
            _check_hresult(
                _com_call(
                    device,
                    3,
                    _HRESULT,
                    [ctypes.POINTER(_GUID), _DWORD, _VOID_P, ctypes.POINTER(_VOID_P)],
                    ctypes.byref(iid_audio_client),
                    _CLSCTX_ALL,
                    None,
                    ctypes.byref(audio_client),
                ),
                "IMMDevice.Activate(IAudioClient)",
            )

            _check_hresult(
                _com_call(
                    audio_client,
                    8,
                    _HRESULT,
                    [ctypes.POINTER(_VOID_P)],
                    ctypes.byref(mix_format),
                ),
                "IAudioClient.GetMixFormat",
            )
            format_info = _WAVEFORMATEX.from_address(mix_format.value)
            sample_rate = int(format_info.nSamplesPerSec)
            channels = int(format_info.nChannels)
            block_align = int(format_info.nBlockAlign)
            bits_per_sample = int(format_info.wBitsPerSample)
            is_float = int(format_info.wFormatTag) == _WAVE_FORMAT_IEEE_FLOAT

            if int(format_info.wFormatTag) == _WAVE_FORMAT_EXTENSIBLE and format_info.cbSize >= 22:
                subtype_address = mix_format.value + 24
                subtype = bytes((ctypes.c_ubyte * 16).from_address(subtype_address))
                is_float = subtype == uuid.UUID(_SUBFORMAT_FLOAT).bytes_le
                if subtype != uuid.UUID(_SUBFORMAT_PCM).bytes_le and not is_float:
                    raise RuntimeError("Unsupported WASAPI shared sample format")

            logger.info(
                "WASAPI loopback config: %sHz, %sch, %s-bit, %s",
                sample_rate,
                channels,
                bits_per_sample,
                "float" if is_float else "pcm",
            )

            _check_hresult(
                _com_call(
                    audio_client,
                    3,
                    _HRESULT,
                    [_DWORD, _DWORD, _REF_TIME, _REF_TIME, _VOID_P, _VOID_P],
                    _AUDCLNT_SHAREMODE_SHARED,
                    _AUDCLNT_STREAMFLAGS_LOOPBACK,
                    10_000_000,
                    0,
                    mix_format,
                    None,
                ),
                "IAudioClient.Initialize(loopback)",
            )

            capture_client = _VOID_P()
            iid_capture_client = _GUID.from_string(_IID_IAUDIO_CAPTURE_CLIENT)
            _check_hresult(
                _com_call(
                    audio_client,
                    14,
                    _HRESULT,
                    [ctypes.POINTER(_GUID), ctypes.POINTER(_VOID_P)],
                    ctypes.byref(iid_capture_client),
                    ctypes.byref(capture_client),
                ),
                "IAudioClient.GetService(IAudioCaptureClient)",
            )
            _check_hresult(
                _com_call(audio_client, 10, _HRESULT, []),
                "IAudioClient.Start",
            )
            logger.info("WASAPI loopback capture started")

            while not stop_event.is_set():
                packet_frames = _UINT32()
                _check_hresult(
                    _com_call(
                        capture_client,
                        5,
                        _HRESULT,
                        [ctypes.POINTER(_UINT32)],
                        ctypes.byref(packet_frames),
                    ),
                    "IAudioCaptureClient.GetNextPacketSize",
                )
                if not packet_frames.value:
                    stop_event.wait(0.005)
                    continue

                data = _VOID_P()
                frames = _UINT32()
                flags = _DWORD()
                _check_hresult(
                    _com_call(
                        capture_client,
                        3,
                        _HRESULT,
                        [
                            ctypes.POINTER(_VOID_P),
                            ctypes.POINTER(_UINT32),
                            ctypes.POINTER(_DWORD),
                            _VOID_P,
                            _VOID_P,
                        ],
                        ctypes.byref(data),
                        ctypes.byref(frames),
                        ctypes.byref(flags),
                        None,
                        None,
                    ),
                    "IAudioCaptureClient.GetBuffer",
                )
                try:
                    if flags.value & _AUDCLNT_BUFFERFLAGS_SILENT:
                        samples = np.zeros((frames.value, channels), dtype=np.float32)
                    else:
                        raw = ctypes.string_at(data, frames.value * block_align)
                        samples = self._decode(raw, frames.value, channels, bits_per_sample, is_float)
                    if samples.size:
                        on_audio(samples.mean(axis=1), sample_rate)
                finally:
                    _check_hresult(
                        _com_call(
                            capture_client,
                            4,
                            _HRESULT,
                            [_UINT32],
                            frames.value,
                        ),
                        "IAudioCaptureClient.ReleaseBuffer",
                    )

            _check_hresult(_com_call(audio_client, 11, _HRESULT, []), "IAudioClient.Stop")
        finally:
            if mix_format.value:
                ole32.CoTaskMemFree(mix_format)
            _release(capture_client)
            _release(audio_client)
            _release(device)
            _release(enumerator)
            if initialized:
                ole32.CoUninitialize()

    @staticmethod
    def _decode(
        raw: bytes,
        frames: int,
        channels: int,
        bits_per_sample: int,
        is_float: bool,
    ) -> np.ndarray:
        if is_float and bits_per_sample == 32:
            values = np.frombuffer(raw, dtype=np.float32)
        elif bits_per_sample == 16:
            values = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        elif bits_per_sample == 32:
            values = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
        else:
            raise RuntimeError(f"Unsupported WASAPI sample width: {bits_per_sample}")
        return values[: frames * channels].reshape(frames, channels)
