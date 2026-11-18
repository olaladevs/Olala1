#!/usr/bin/env python3
"""
render.py
Pipeline: script.json -> TTS (edge-tts) -> FFmpeg compose per scene -> concat -> tambah BGM

Struktur folder yang dibutuhkan:
  project/
    script.json
    render.py
    assets/
      video/   <- visual tiap scene (hook_lightning_3d.mp4, battle_1_catur.mp4, dst)
      sfx/     <- sound effect tiap scene
      bgm/     <- background music (opsional, satu file buat seluruh video)
    audio/     <- auto-generated: hasil TTS per scene
    build/     <- auto-generated: hasil render per scene & output final
"""

import asyncio
import json
import subprocess
import os
import sys
from pathlib import Path

import edge_tts
from pydub import AudioSegment

# ====== KONFIGURASI ======
VOICE = "id-ID-ArdiNeural"       # alternatif: "id-ID-GadisNeural"
TTS_RATE = "+18%"                # kecepatan narasi, sesuai pacing TikTok
OUTPUT_RESOLUTION = "1080x1920"  # 9:16 buat TikTok/Reels/Shorts
BGM_PATH = "assets/bgm/background_music.mp3"
BGM_VOLUME = 0.15
FINAL_OUTPUT = "build/output_final.mp4"

SCRIPT_JSON = "script.json"
AUDIO_DIR = Path("audio")
BUILD_DIR = Path("build")


def load_script(path=SCRIPT_JSON):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return sorted(data, key=lambda s: s["order"])


async def generate_tts(text: str, out_path: Path):
    communicate = edge_tts.Communicate(text, VOICE, rate=TTS_RATE)
    await communicate.save(str(out_path))


async def generate_all_tts(scenes):
    AUDIO_DIR.mkdir(exist_ok=True)
    for s in scenes:
        out_path = AUDIO_DIR / f"{s['scene']}.mp3"
        if out_path.exists():
            print(f"[TTS] skip (udah ada): {out_path}")
            continue
        print(f"[TTS] generate: {out_path}")
        await generate_tts(s["narration"], out_path)


def get_duration_seconds(path) -> float:
    audio = AudioSegment.from_file(path)
    return len(audio) / 1000.0


def run(cmd):
    print("  $", " ".join(cmd))
    subprocess.run(cmd, check=True)


def build_scene_video(visual_path, audio_path, duration, out_path):
    """
    Loop visual (kalau videonya lebih pendek dari narasi) dan potong pas durasi narasi.
    Scale + pad ke resolusi target biar semua scene konsisten 9:16.
    """
    w, h = OUTPUT_RESOLUTION.split("x")
    vf = (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black"
    )
    cmd = [
        "ffmpeg", "-y",
        "-stream_loop", "-1", "-i", str(visual_path),
        "-i", str(audio_path),
        "-t", str(duration),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        str(out_path),
    ]
    run(cmd)


def mix_sfx_into_scene(scene_video_path, sfx_paths, out_path):
    """
    Opsional: tumpuk SFX di atas audio narasi yang udah ada di video scene.
    Kalau sfx_paths kosong, cukup copy file.
    """
    if not sfx_paths:
        run(["cp", str(scene_video_path), str(out_path)])
        return

    inputs = ["-i", str(scene_video_path)]
    for sfx in sfx_paths:
        inputs += ["-i", str(sfx)]

    n = len(sfx_paths)
    amix_inputs = "[0:a]" + "".join(f"[{i+1}:a]" for i in range(n))
    filter_complex = f"{amix_inputs}amix=inputs={n+1}:duration=first:dropout_transition=0[aout]"

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac",
        str(out_path),
    ]
    run(cmd)


def concat_scenes(scene_files, out_path):
    list_path = BUILD_DIR / "concat_list.txt"
    with open(list_path, "w") as f:
        for p in scene_files:
            f.write(f"file '{Path(p).resolve()}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(list_path),
        "-c", "copy",
        str(out_path),
    ]
    run(cmd)


def add_bgm(video_path, bgm_path, out_path, bgm_volume=BGM_VOLUME):
    if not Path(bgm_path).exists():
        print(f"[BGM] skip, file nggak ketemu: {bgm_path}")
        run(["cp", str(video_path), str(out_path)])
        return

    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-stream_loop", "-1", "-i", str(bgm_path),
        "-filter_complex",
        f"[1:a]volume={bgm_volume}[bgm];"
        f"[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=0[aout]",
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac",
        "-shortest",
        str(out_path),
    ]
    run(cmd)


def main():
    BUILD_DIR.mkdir(exist_ok=True)
    scenes = load_script()

    # 1. Generate semua narasi TTS
    print("\n=== STEP 1: Generate TTS ===")
    asyncio.run(generate_all_tts(scenes))

    # 2. Build tiap scene (visual + narasi, durasi ngikut panjang narasi asli audio)
    print("\n=== STEP 2: Build video per scene ===")
    scene_outputs = []
    for s in scenes:
        audio_path = AUDIO_DIR / f"{s['scene']}.mp3"
        visual_path = Path(s["visual_asset"])

        if not visual_path.exists():
            print(f"[WARNING] visual asset belum ada: {visual_path} -> skip scene ini")
            continue

        duration = get_duration_seconds(audio_path)
        print(f"[{s['scene']}] durasi narasi: {duration:.2f}s")

        raw_out = BUILD_DIR / f"{s['scene']}_raw.mp4"
        build_scene_video(visual_path, audio_path, duration, raw_out)

        sfx_paths = [p for p in s.get("sfx", []) if Path(p).exists()]
        final_scene_out = BUILD_DIR / f"{s['scene']}_final.mp4"
        mix_sfx_into_scene(raw_out, sfx_paths, final_scene_out)

        scene_outputs.append(final_scene_out)

    if not scene_outputs:
        print("Nggak ada scene yang berhasil dibangun. Cek assets/video/ dulu.")
        sys.exit(1)

    # 3. Gabung semua scene
    print("\n=== STEP 3: Concat semua scene ===")
    concat_path = BUILD_DIR / "concat_output.mp4"
    concat_scenes(scene_outputs, concat_path)

    # 4. Tambah BGM
    print("\n=== STEP 4: Tambah background music ===")
    final_path = Path(FINAL_OUTPUT)
    add_bgm(concat_path, BGM_PATH, final_path)

    print(f"\n✅ SELESAI. Output final: {final_path.resolve()}")


if __name__ == "__main__":
    main()
