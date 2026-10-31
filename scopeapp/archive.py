"""无 pickle 的压缩波形归档：精确采样值 + JSON 元数据，用于离线分析。"""
from __future__ import annotations
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import tempfile
import zipfile
import numpy as np
from .model import ChannelSettings, Coupling, TimebaseSettings, TriggerSettings, Waveform


DEFAULT_MAX_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
_SAMPLE_RATE_TOLERANCE = 1e-6


def _record_metadata(waveforms: dict[str, Waveform], settings: dict) -> dict:
    """Describe what was actually read; unknown device settings remain unknown."""
    tb = settings.get("timebase", TimebaseSettings())
    depth = int(tb.record_length)
    rate = float(tb.sample_rate)
    known_depth = depth > 0
    known_rate = np.isfinite(rate) and rate > 0
    channels = settings.get("channels", {})
    enabled = {name for name, ch in channels.items() if ch.enabled}
    matching_channels = set(waveforms) == enabled if channels else None
    records = {}
    for name, wf in waveforms.items():
        points = int(wf.y.size)
        adapter_depth = int(getattr(wf, "device_record_length", 0))
        adapter_stride = int(getattr(wf, "sample_stride", 0))
        adapter_full = getattr(wf, "full_record", None)
        effective_depth = adapter_depth if adapter_depth > 0 else depth if known_depth else None
        interval_stride = rate * float(wf.x_incr) if known_rate else None
        if interval_stride is not None and not np.isfinite(interval_stride):
            interval_stride = None
        stride = adapter_stride if adapter_stride > 0 else interval_stride
        native_interval = (np.isclose(interval_stride, 1.0, rtol=_SAMPLE_RATE_TOLERANCE,
                                      atol=1e-12) if interval_stride is not None else None)
        if adapter_full is False or adapter_stride > 1:
            full = False
        elif effective_depth is not None and points != effective_depth:
            full = False
        elif effective_depth is not None and native_interval is not None:
            full = bool(native_interval)
        elif adapter_full is True and effective_depth is not None and adapter_stride == 1:
            full = True
        else:
            full = None
        if adapter_depth > 0 and known_depth and adapter_depth != depth:
            full = False
        records[name] = {"points": points,
                         "device_record_length": effective_depth,
                         "sample_stride": stride, "full_record": full,
                         "adapter_record_length": adapter_depth,
                         "adapter_sample_stride": adapter_stride,
                         "adapter_full_record": adapter_full,
                         "completeness_basis": "adapter" if adapter_full is not None
                         else "device_settings" if known_depth and known_rate else "unknown"}
        if getattr(wf, "record_stable", None) is not None:
            records[name]["record_stable"] = wf.record_stable
    states = [info["full_record"] for info in records.values()]
    if matching_channels is False or any(value is False for value in states):
        completeness = "incomplete"
    elif (known_depth and known_rate and matching_channels is True and states
          and all(value is True for value in states)):
        completeness = "complete"
    else:
        completeness = "unknown"
    if any(getattr(wf, "record_stable", None) is False for wf in waveforms.values()):
        completeness = "unstable"
    return {"device_record_length": depth if known_depth else None,
            "sample_rate": rate if known_rate else None,
            "enabled_channels": sorted(enabled) if channels else None,
            "channel_set_complete": matching_channels,
            "completeness": completeness, "channels": records}


def _require_complete(waveforms: dict[str, Waveform], settings: dict, record: dict) -> None:
    if not settings.get("channels"):
        raise ValueError("缺少示波器通道设置，无法确认完整记录")
    expected = set(record["enabled_channels"])
    if set(waveforms) != expected:
        missing, extra = expected - set(waveforms), set(waveforms) - expected
        details = []
        if missing:
            details.append("缺少已开启通道 " + ", ".join(sorted(missing)))
        if extra:
            details.append("包含未开启通道 " + ", ".join(sorted(extra)))
        raise ValueError("波形不是全部已开启通道的完整记录：" + "；".join(details))
    if record["device_record_length"] is None:
        raise ValueError("缺少示波器实际存储深度，无法确认完整记录")
    if record["sample_rate"] is None:
        raise ValueError("缺少示波器实际采样率，无法确认完整采样间隔")
    for name, wf in waveforms.items():
        if getattr(wf, "record_stable", None) is False:
            raise ValueError("当前为运行中的实时读回，不能确认各通道属于同次采集；请手动 STOP 或等待 Single 完成后，再保存完整 NPZ。软件不会自动停止示波器。")
        info = record["channels"][name]
        if info["adapter_record_length"] > 0 and info["adapter_record_length"] != record["device_record_length"]:
            raise ValueError(f"{name} 波形对应存储深度 {info['adapter_record_length']}，"
                             f"保存设置为 {record['device_record_length']}；波形与设备设置不属于同一记录")
        if info["points"] != record["device_record_length"]:
            raise ValueError(f"{name} 仅有 {info['points']} 个真实采样点，"
                             f"设备存储深度为 {record['device_record_length']}；拒绝保存为完整记录")
        if info["full_record"] is not True:
            if info["adapter_full_record"] is False or info["adapter_sample_stride"] > 1:
                raise ValueError(f"{name} 是设备抽点预览，拒绝保存为完整记录")
            raise ValueError(f"{name} 采样间隔 {wf.x_incr:g} s 与设备采样率 "
                             f"{record['sample_rate']:g} Sa/s 不符，可能是抽点预览；拒绝保存为完整记录")
        if wf.y.ndim != 1 or wf.y.size < 2 or not _finite_samples(wf.y):
            raise ValueError(f"{name} 采样数据无效")
        if not np.isfinite(wf.x_start):
            raise ValueError(f"{name} 时间轴无效")
        if wf.raw is not None and (wf.raw.ndim != 1 or wf.raw.size != wf.y.size):
            raise ValueError(f"{name} 原始采样码点数与电压数据不符")


def _finite_samples(values: np.ndarray) -> bool:
    # Avoid allocating a record-sized boolean array for very deep acquisitions.
    return all(np.all(np.isfinite(values[start:start + 1_000_000]))
               for start in range(0, values.size, 1_000_000))


def save_archive(path: str | Path, waveforms: dict[str, Waveform], settings: dict,
                 identity: str = "", processing: dict | None = None,
                 *, require_complete: bool = False) -> str:
    if not waveforms:
        raise ValueError("当前没有可保存的波形")
    for name, wf in waveforms.items():
        if wf.y.dtype.kind not in "iuf" or (wf.raw is not None and wf.raw.dtype.kind not in "iuf"):
            raise ValueError(f"{name} 波形或原始采样码类型无效；归档不允许 pickle 数据")
    record = _record_metadata(waveforms, settings)
    if require_complete:
        _require_complete(waveforms, settings, record)
    metadata = {
        "format": "ScopeHost", "version": 1,
        "created": datetime.now(timezone.utc).isoformat(), "identity": identity,
        "channels": {k: asdict(v) for k, v in settings.get("channels", {}).items()},
        "timebase": asdict(settings.get("timebase", TimebaseSettings())),
        "trigger": asdict(settings.get("trigger", TriggerSettings())),
        "processing": processing or {}, "waveforms": {}, "acquisition": record,
    }
    arrays = {}
    for i, (name, wf) in enumerate(waveforms.items()):
        key = f"wave_{i}"
        arrays[key] = wf.y
        metadata["waveforms"][name] = {"key": key, "x_start": wf.x_start,
            "x_incr": wf.x_incr, "encoding": wf.source_encoding, "note": wf.decode_note,
            **record["channels"][name]}
        if wf.raw is not None:
            raw_key = f"raw_{i}"
            arrays[raw_key] = wf.raw
            metadata["waveforms"][name]["raw_key"] = raw_key
    arrays["metadata"] = np.array(json.dumps(metadata, ensure_ascii=False))
    path = Path(path).with_suffix(".npz")
    # The previous archive survives validation, compression, and disk-write failures.
    # A temporary file on the same filesystem makes the final replacement atomic.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=f".{path.stem}-",
                                         suffix=".npz.tmp", dir=path.parent,
                                         delete=False) as out:
            temporary = Path(out.name)
            np.savez_compressed(out, **arrays)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return str(path)


def load_archive(path: str | Path, *,
                 max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES
                 ) -> tuple[dict[str, Waveform], dict, dict]:
    if max_uncompressed_bytes <= 0:
        raise ValueError("归档解压大小上限必须为正数")
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len({item.filename for item in entries}) != len(entries):
            raise ValueError("波形归档包含重复数据文件")
        if sum(item.file_size for item in entries) > max_uncompressed_bytes:
            raise ValueError(f"波形归档解压后超过 {max_uncompressed_bytes / (1024 ** 2):g} MB")
        if not any(item.filename == "metadata.npy" for item in entries):
            raise ValueError("波形归档缺少元数据")
        if any(item.filename == "metadata.npy" and item.file_size > 2 * 1024 * 1024
               for item in entries):
            raise ValueError("波形归档元数据过大")
    with np.load(path, allow_pickle=False) as data:
        meta = json.loads(str(data["metadata"].item()))
        if meta.get("format") != "ScopeHost" or meta.get("version") != 1:
            raise ValueError("不支持的波形归档格式")
        waveforms = {}
        for name, info in meta["waveforms"].items():
            stored_y = data[info["key"]]
            if stored_y.dtype.kind not in "iuf":
                raise ValueError(f"{name} 采样数据类型无效")
            y = np.asarray(stored_y, dtype=np.float64)
            x_start, x_incr = float(info["x_start"]), float(info["x_incr"])
            if y.ndim != 1 or y.size < 2 or not _finite_samples(y):
                raise ValueError(f"{name} 采样数据无效")
            if not np.isfinite(x_start) or not np.isfinite(x_incr) or x_incr <= 0:
                raise ValueError(f"{name} 时间轴无效")
            if "points" in info and (type(info["points"]) is not int or info["points"] != y.size):
                raise ValueError(f"{name} 元数据声明的采样点数与真实数组不符")
            raw = data[info["raw_key"]] if "raw_key" in info else None
            if raw is not None and (raw.ndim != 1 or raw.size != y.size
                                    or raw.dtype.kind not in "iuf"):
                raise ValueError(f"{name} 原始采样码点数或类型无效")
            # np.load returns owning arrays; copying them doubles deep-record memory.
            waveforms[name] = Waveform(name, y, x_start, x_incr,
                raw=raw,
                source_encoding=info.get("encoding", ""), decode_note=info.get("note", ""))
            for attribute, key in (("device_record_length", "adapter_record_length"),
                                   ("sample_stride", "adapter_sample_stride"),
                                   ("full_record", "adapter_full_record"),
                                   ("record_stable", "record_stable")):
                if key in info:
                    setattr(waveforms[name], attribute, info[key])
        if not waveforms:
            raise ValueError("归档没有波形")
    channels = {}
    for name, info in meta["channels"].items():
        info = dict(info)
        info["coupling"] = Coupling(info.get("coupling", "DC"))
        if not np.isfinite(info["scale"]) or info["scale"] <= 0 or not np.isfinite(info["offset"]):
            raise ValueError("通道量程或偏移无效")
        channels[name] = ChannelSettings(**info)
    tb = TimebaseSettings(**meta["timebase"])
    if not np.isfinite(tb.scale) or tb.scale <= 0 or not np.isfinite(tb.position):
        raise ValueError("时基设置无效")
    settings = {"channels": channels, "timebase": tb,
                "trigger": TriggerSettings(**meta["trigger"])}
    if "acquisition" in meta:
        actual_record = _record_metadata(waveforms, settings)
        claimed_record = meta["acquisition"]
        if claimed_record != actual_record:
            raise ValueError("归档采样真实性元数据与实际采样数组或设备设置不符")
        for name, info in meta["waveforms"].items():
            actual = actual_record["channels"][name]
            if any(info.get(key) != value for key, value in actual.items()):
                raise ValueError(f"{name} 采样真实性声明与实际数据不符")
        if claimed_record["completeness"] == "complete":
            _require_complete(waveforms, settings, actual_record)
    else:
        # Version 1 archives created before integrity metadata have no evidence of
        # complete device memory. Do not infer completeness from array size alone.
        meta["acquisition"] = {"completeness": "unknown", "channels": {
            name: {"points": wf.points, "full_record": None, "sample_stride": None,
                   "device_record_length": None} for name, wf in waveforms.items()}}
    return waveforms, settings, meta
