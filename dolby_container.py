#!/usr/bin/env python3
"""Inspect Dolby signaling in an MP4 without reading its media payload.

``inspect_container(path)`` returns JSON-compatible metadata; malformed or
truncated structures raise ``ContainerError``. A dec3 Atmos flag or DV config
record is *signaling*, not proof that valid JOC/RPU data exists in every sample.
Use a separate bitstream probe and playback test for that.

Bit layouts are based on primary sources:
* ETSI TS 102 366 V1.4.1, Annex F.6 (EC3SpecificBox):
  https://www.etsi.org/deliver/etsi_ts/102300_102399/102366/01.04.01_60/ts_102366v010401p.pdf
* ETSI TS 103 420 V1.2.1, Annex C.3 (extension flag and complexity):
  https://www.etsi.org/deliver/etsi_ts/103400_103499/103420/01.02.01_60/ts_103420v010201p.pdf
* FFmpeg libavformat/dovi_isom.c (dvcC/dvvC configuration record):
  https://github.com/FFmpeg/FFmpeg/blob/master/libavformat/dovi_isom.c
* Apple's HLS authoring specification appendixes (hvc1/ec-3/JOC):
  https://developer.apple.com/documentation/http-live-streaming/hls-authoring-specification-for-apple-devices-appendixes
* Apple TN3145 (Profile 8.4 ambient viewing environment requirements):
  https://developer.apple.com/documentation/technotes/tn3145-hdr-video-metadata

No external Python dependencies are required.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import struct
import sys
from typing import BinaryIO, Iterator


class ContainerError(ValueError):
    """Malformed, truncated, or unsupported MP4 metadata."""


@dataclass(frozen=True)
class _Box:
    type: str
    offset: int
    size: int
    header_size: int

    @property
    def payload(self) -> int:
        return self.offset + self.header_size

    @property
    def end(self) -> int:
        return self.offset + self.size

    def as_dict(self) -> dict:
        return {"type": self.type, "offset": self.offset, "size": self.size,
                "header_size": self.header_size}


class _Reader:
    # These are bounded metadata reads, never mdat reads. Most reads are < 128 B.
    MAX_METADATA_BYTES = 1024 * 1024
    MAX_BOXES = 100_000

    def __init__(self, file: BinaryIO, size: int):
        self.file = file
        self.size = size
        self.boxes_seen = 0

    def read(self, offset: int, length: int, end: int | None = None) -> bytes:
        if end is None:
            end = self.size
        if length < 0 or offset < 0 or offset + length > end or end > self.size:
            raise ContainerError(f"Truncated metadata at offset {offset}")
        if length > self.MAX_METADATA_BYTES:
            raise ContainerError(f"Metadata read exceeds limit at offset {offset}")
        self.file.seek(offset)
        data = self.file.read(length)
        if len(data) != length:
            raise ContainerError(f"File changed or truncated at offset {offset}")
        return data

    def boxes(self, start: int, end: int) -> Iterator[_Box]:
        current = start
        while current < end:
            if end - current < 8:
                raise ContainerError(f"Truncated box header at offset {current}")
            size, fourcc = struct.unpack(">I4s", self.read(current, 8, end))
            header_size = 8
            if size == 1:
                size = struct.unpack(">Q", self.read(current + 8, 8, end))[0]
                header_size = 16
            elif size == 0:
                size = end - current
            if fourcc == b"uuid":
                header_size += 16
            if size < header_size or current + size > end:
                raise ContainerError(f"Invalid {fourcc!r} box size {size} at {current}")
            self.boxes_seen += 1
            if self.boxes_seen > self.MAX_BOXES:
                raise ContainerError("MP4 has too many boxes")
            box = _Box(fourcc.decode("latin-1"), current, size, header_size)
            yield box
            current = box.end

    def payload(self, box: _Box) -> bytes:
        return self.read(box.payload, box.size - box.header_size, box.end)


class _Bits:
    def __init__(self, data: bytes):
        self.data = data
        self.position = 0

    @property
    def remaining(self) -> int:
        return len(self.data) * 8 - self.position

    def read(self, count: int) -> int:
        if count < 0 or count > self.remaining:
            raise ContainerError("Truncated dec3 bit fields")
        value = 0
        for _ in range(count):
            value = value << 1 | (self.data[self.position // 8] >>
                                  (7 - self.position % 8)) & 1
            self.position += 1
        return value


def parse_dec3(data: bytes) -> dict:
    """Parse EC3SpecificBox *payload*, including optional Atmos signaling."""
    bits = _Bits(data)
    data_rate = bits.read(13)
    independent_count = bits.read(3) + 1
    substreams = []
    channel_counts = (2, 1, 2, 3, 3, 4, 4, 5)
    for _ in range(independent_count):
        fscod, bsid = bits.read(2), bits.read(5)
        bits.read(1)  # reserved
        asvc, bsmod, acmod, lfeon = (bits.read(1), bits.read(3),
                                    bits.read(3), bits.read(1))
        bits.read(3)  # reserved
        dependent_count = bits.read(4)
        if dependent_count:
            chan_loc = bits.read(9)
        else:
            bits.read(1)  # reserved (not chan_loc)
            chan_loc = None
        substreams.append({
            "fscod": fscod, "sample_rate": (48000, 44100, 32000, None)[fscod],
            "bsid": bsid, "asvc": asvc, "bsmod": bsmod, "acmod": acmod,
            "lfeon": lfeon, "base_channels": channel_counts[acmod] + lfeon,
            "num_dep_sub": dependent_count, "chan_loc": chan_loc,
        })
    # Each independent substream ends on a byte boundary (24 or 32 bits).
    extension_present = bits.remaining >= 16
    extension_flag = False
    complexity = None
    if bits.remaining:
        if bits.remaining < 8:
            raise ContainerError("Truncated dec3 extension byte")
        bits.read(7)  # reserved
        extension_flag = bool(bits.read(1))
        if bits.remaining < 8:
            if extension_flag:
                raise ContainerError("Atmos flag set but dec3 complexity index is truncated")
        else:
            complexity = bits.read(8)
    return {
        "data_rate_kbps": data_rate,
        "num_ind_sub": independent_count - 1,
        "independent_substream_count": independent_count,
        "substreams": substreams,
        "extension_present": extension_present,
        "flag_ec3_extension_type_a": extension_flag,
        # FFmpeg calls the same standardized bit flag_eac3_extension_type_a.
        "complexity_index_type_a": complexity,
        "atmos_signaled": extension_flag,
        "atmos_signaling_valid": extension_flag and complexity is not None
                                   and 1 <= complexity <= 16,
        "payload_hex": data.hex(),
    }


def _parse_dovi(data: bytes, box_type: str) -> dict:
    if len(data) < 4:
        raise ContainerError(f"Truncated {box_type} Dolby Vision config")
    flags = int.from_bytes(data[2:4], "big")
    return {
        "box_type": box_type,
        "dv_version_major": data[0], "dv_version_minor": data[1],
        "dv_profile": flags >> 9 & 0x7F, "dv_level": flags >> 3 & 0x3F,
        "rpu_present_flag": flags >> 2 & 1,
        "el_present_flag": flags >> 1 & 1, "bl_present_flag": flags & 1,
        "dv_bl_signal_compatibility_id": data[4] >> 4 if len(data) >= 5 else 0,
        "dv_md_compression": data[4] >> 2 & 3 if len(data) >= 5 else 0,
        "payload_hex": data.hex(),
    }


def _parse_colr(data: bytes) -> dict:
    if len(data) < 4:
        raise ContainerError("Truncated colr type")
    color_type = data[:4].decode("latin-1")
    result = {"color_type": color_type}
    if color_type in ("nclx", "nclc"):
        required = 11 if color_type == "nclx" else 10
        if len(data) < required:
            raise ContainerError(f"Truncated colr {color_type} fields")
        primaries, transfer, matrix = struct.unpack(">HHH", data[4:10])
        result.update({"color_primaries": primaries,
                       "transfer_characteristics": transfer,
                       "matrix_coefficients": matrix})
        if color_type == "nclx":
            result["full_range_flag"] = bool(data[10] & 0x80)
    return result


def _parse_amve(data: bytes) -> dict:
    # amve is a Box, not a FullBox. Apple TN3145 and FFmpeg mov_read_amve
    # define exactly 32 + 16 + 16 bits; no version/flags prefix exists.
    if len(data) != 8:
        raise ContainerError("Invalid/truncated amve payload: expected 8 bytes")
    illuminance, light_x, light_y = struct.unpack(">IHH", data)
    return {
        "present": True, "payload_hex": data.hex(),
        "ambient_illuminance_raw": illuminance,
        "ambient_light_x_raw": light_x, "ambient_light_y_raw": light_y,
        "ambient_illuminance_lux": illuminance / 10000,
        "ambient_light_x_normalized": light_x / 50000,
        "ambient_light_y_normalized": light_y / 50000,
    }


def _parse_hvcc(data: bytes) -> dict:
    if len(data) < 23:
        raise ContainerError("Truncated hvcC configuration")
    result = {"configuration_version": data[0], "profile_idc": data[1] & 31,
              "tier_flag": bool(data[1] & 32), "level_idc": data[12],
              "chroma_format_idc": data[16] & 3,
              "bit_depth_luma": (data[17] & 7) + 8,
              "bit_depth_chroma": (data[18] & 7) + 8,
              "nal_length_size": (data[21] & 3) + 1, "nal_arrays": []}
    position = 23
    for _ in range(data[22]):
        if position + 3 > len(data):
            raise ContainerError("Truncated hvcC NAL array")
        flags, count = data[position], int.from_bytes(data[position + 1:position + 3], "big")
        position += 3
        lengths = []
        for _ in range(count):
            if position + 2 > len(data):
                raise ContainerError("Truncated hvcC NAL length")
            length = int.from_bytes(data[position:position + 2], "big")
            position += 2
            if position + length > len(data):
                raise ContainerError("Truncated hvcC NAL payload")
            lengths.append(length)
            position += length
        result["nal_arrays"].append({"nal_unit_type": flags & 63,
                                      "array_completeness": bool(flags & 128),
                                      "nal_count": count, "nal_lengths": lengths})
    return result


def _sample_entry(reader: _Reader, box: _Box, index: int) -> dict:
    result = {"index": index, **box.as_dict(), "child_boxes": []}
    if box.type in ("hvc1", "hev1", "dvh1", "dvhe", "avc1", "avc3", "encv"):
        prefix = reader.read(box.payload, 78, box.end)
        result.update({"kind": "video", "width": int.from_bytes(prefix[24:26], "big"),
                       "height": int.from_bytes(prefix[26:28], "big"),
                       "ambient_viewing_environment": {"present": False}})
        child_start = box.payload + 78
    elif box.type in ("ec-3", "ac-3", "mp4a", "enca", "alac", "fLaC"):
        prefix = reader.read(box.payload, 28, box.end)
        version = int.from_bytes(prefix[8:10], "big")
        if version not in (0, 1, 2):
            raise ContainerError(f"Unsupported audio sample entry version {version}")
        result.update({"kind": "audio", "sample_entry_version": version,
                       # EC3 channel_count is not authoritative (ETSI F.5.2).
                       "sample_entry_channel_count": int.from_bytes(prefix[16:18], "big"),
                       "sample_rate": int.from_bytes(prefix[24:28], "big") / 65536})
        child_start = box.payload + {0: 28, 1: 44, 2: 64}[version]
        if child_start > box.end:
            raise ContainerError("Truncated versioned audio sample entry")
    else:
        result["kind"] = "unparsed"
        return result
    for child in reader.boxes(child_start, box.end):
        result["child_boxes"].append(child.as_dict())
        if child.type == "dec3":
            result["dec3"] = parse_dec3(reader.payload(child))
        elif child.type in ("dvcC", "dvvC", "dvwC"):
            result["dolby_vision"] = _parse_dovi(reader.payload(child), child.type)
        elif child.type == "colr":
            result["colr"] = _parse_colr(reader.payload(child))
        elif child.type == "amve":
            result["ambient_viewing_environment"] = _parse_amve(reader.payload(child))
        elif child.type == "hvcC":
            result["hvcC"] = _parse_hvcc(reader.payload(child))
    return result


def _track(reader: _Reader, trak: _Box) -> dict:
    result = {"offset": trak.offset, "sample_entries": []}
    for box in reader.boxes(trak.payload, trak.end):
        if box.type == "tkhd":
            version = reader.read(box.payload, 1, box.end)[0]
            if version not in (0, 1):
                raise ContainerError(f"Unsupported tkhd version {version}")
            track_id_offset = box.payload + (20 if version == 1 else 12)
            result["track_id"] = int.from_bytes(reader.read(track_id_offset, 4, box.end), "big")
        elif box.type == "mdia":
            for media in reader.boxes(box.payload, box.end):
                if media.type == "hdlr":
                    result["handler_type"] = reader.read(media.payload + 8, 4, media.end).decode("latin-1")
                elif media.type == "mdhd":
                    version = reader.read(media.payload, 1, media.end)[0]
                    if version not in (0, 1):
                        raise ContainerError(f"Unsupported mdhd version {version}")
                    scale_offset = media.payload + (20 if version == 1 else 12)
                    scale = int.from_bytes(reader.read(scale_offset, 4, media.end), "big")
                    duration = int.from_bytes(reader.read(scale_offset + 4, 8 if version else 4, media.end), "big")
                    result.update({"media_timescale": scale, "media_duration_units": duration,
                                   "duration_seconds": duration / scale if scale else None})
                elif media.type == "minf":
                    for minimum in reader.boxes(media.payload, media.end):
                        if minimum.type != "stbl":
                            continue
                        for table in reader.boxes(minimum.payload, minimum.end):
                            if table.type == "stsd":
                                prefix = reader.read(table.payload, 8, table.end)
                                if prefix[0] != 0:
                                    raise ContainerError("Unsupported stsd version")
                                count = int.from_bytes(prefix[4:8], "big")
                                entries = list(reader.boxes(table.payload + 8, table.end))
                                if count != len(entries):
                                    raise ContainerError("stsd entry_count does not match sample entries")
                                result["sample_entries"] = [
                                    _sample_entry(reader, entry, index + 1)
                                    for index, entry in enumerate(entries)]
    return result


def inspect_container(path: str | Path) -> dict:
    """Return bounded, seek-based MP4 inspection; no audio/video samples read."""
    path = Path(path).expanduser().resolve()
    with path.open("rb") as file:
        size = file.seek(0, 2)
        reader = _Reader(file, size)
        top = list(reader.boxes(0, size))
        moovs = [box for box in top if box.type == "moov"]
        if not moovs:
            raise ContainerError("MP4 has no moov box")
        if len(moovs) != 1:
            raise ContainerError("MP4 must have exactly one moov box")
        tracks = [_track(reader, box)
                  for box in reader.boxes(moovs[0].payload, moovs[0].end)
                  if box.type == "trak"]
        mdats = [box for box in top if box.type == "mdat"]
        faststart = moovs[0].offset < mdats[0].offset if mdats else None
        entries = [entry for track in tracks for entry in track["sample_entries"]]
        dv = [entry for entry in entries if "dolby_vision" in entry]
        ec3 = [(track, entry) for track in tracks for entry in track["sample_entries"]
               if entry["type"] == "ec-3"]
        atmos = [entry for _, entry in ec3 if entry.get("dec3", {}).get("atmos_signaled")]
        warnings = []
        if dv and any(entry["type"] not in ("hvc1", "dvh1") for entry in dv):
            warnings.append("Dolby Vision HEVC sample entry is not Apple's recommended hvc1/dvh1")
        for entry in dv:
            config = entry["dolby_vision"]
            if (config["dv_profile"] == 8 and config["dv_bl_signal_compatibility_id"] == 4
                    and not entry.get("ambient_viewing_environment", {}).get("present")):
                warnings.append(
                    "Dolby Vision Profile 8.4 sample entry has no amve ambient viewing environment atom; "
                    "Apple TN3145 requires it for this profile. This file alone does not show whether "
                    "the source lacked it or a remux removed it; compare the downloaded source. "
                    "Do not invent ambient values; native tone mapping requires a playback check."
                )
        for track, entry in ec3:
            dec3 = entry.get("dec3")
            if dec3 is None:
                warnings.append("ec-3 sample entry has no dec3 configuration box")
            elif dec3["atmos_signaled"] and not dec3["atmos_signaling_valid"]:
                warnings.append("dec3 Atmos flag has an invalid/missing complexity index")
            if entry["sample_rate"] != track.get("media_timescale"):
                warnings.append("ec-3 sample rate differs from the media timescale")
        result = {
            "path": str(path), "size_bytes": size,
            "top_level_boxes": [box.as_dict() for box in top], "faststart": faststart,
            "tracks": tracks, "dolby_vision_signaled": bool(dv),
            "atmos_signaled": bool(atmos),
            "apple_container_checks": {
                "preferred_dv_hevc_sample_entry": bool(dv) and all(
                    entry["type"] in ("hvc1", "dvh1") for entry in dv),
                "dolby_vision_rpu_signaled": bool(dv) and all(
                    entry["dolby_vision"]["rpu_present_flag"] == 1 for entry in dv),
                "dolby_vision_base_layer_signaled": bool(dv) and all(
                    entry["dolby_vision"]["bl_present_flag"] == 1 for entry in dv),
                "ec3_joc_signaling_valid": bool(atmos) and all(
                    entry["dec3"]["atmos_signaling_valid"] for entry in atmos),
                "ec3_sample_rate_matches_media_timescale": bool(ec3) and all(
                    entry["sample_rate"] == track.get("media_timescale") for track, entry in ec3),
                "moov_before_mdat": faststart,
            },
            "warnings": warnings, "payload_verification": "not_performed",
            "playback_verification": "not_performed",
        }
        ftyp = next((box for box in top if box.type == "ftyp"), None)
        if ftyp:
            brands = reader.payload(ftyp)
            if len(brands) < 8 or len(brands) % 4:
                raise ContainerError("Invalid ftyp brands")
            result["file_type"] = {
                "major_brand": brands[:4].decode("latin-1"),
                "minor_version": int.from_bytes(brands[4:8], "big"),
                "compatible_brands": [brands[i:i + 4].decode("latin-1")
                                      for i in range(8, len(brands), 4)]}
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="+", help="MP4/M4A file(s) to inspect")
    args = parser.parse_args(argv)
    results = []
    failed = False
    for path in args.paths:
        try:
            results.append(inspect_container(path))
        except (OSError, ContainerError) as error:
            failed = True
            results.append({"path": str(Path(path).expanduser().absolute()), "error": str(error)})
    json.dump(results[0] if len(results) == 1 else results, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
