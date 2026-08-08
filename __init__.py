"""ComfyUI-PreviewWebM.

Play an image batch back as a scrubable video, inline in the node, without
saving anything.

ComfyUI core has no preview-video node — only ``SaveWEBM`` and ``SaveVideo``,
which both write into ``output/`` and leave a file behind every time you queue.
This node encodes VP9/WebM into the temp directory instead, hands the frontend a
``PreviewVideo`` descriptor, and deletes its own previous file on the next run.
Nothing accumulates in ``output/`` and nothing needs cleaning up afterwards.

The frontend's rule for showing a real ``<video>`` element rather than an
animated-image widget is narrow: the UI payload must carry a truthy ``animated``
*array*, and no filename in it may end in ``.png`` or ``.webp``. A ``.webm``
satisfies both, and ``/view`` serves it through aiohttp's ``FileResponse``, which
honours range requests — so the scrub bar actually seeks instead of reloading.

The quality presets exist because libvpx-vp9's defaults are tuned for archival
encodes, not for a file you look at once and throw away. On 60 frames of 1080p,
the default settings take 1.9 s where the realtime path takes 0.3 s. Previews
should not cost more than the sampling did.
"""

import contextlib
import math
import os
import uuid
from fractions import Fraction

import av
import torch
from typing_extensions import override

import folder_paths
from comfy.utils import ProgressBar
from comfy_api.latest import ComfyExtension, IO, UI

__version__ = "1.0.0"


# ---------------------------------------------------------------------------
# Encoder presets
# Maps the quality combo to (crf, deadline, cpu-used, keyframe interval in
# seconds). Lower cpu-used means more search effort and a slower encode; the
# keyframe interval sets how far a scrub-bar drag has to seek back.
# ---------------------------------------------------------------------------
_PRESETS = {
    "fast":     ("40", "realtime", "8", 1),
    "balanced": ("34", "good",     "5", 1),
    "high":     ("28", "good",     "2", 2),
}


# ---------------------------------------------------------------------------
# Audio
# WebM accepts only Vorbis and Opus, so AAC — what the core VIDEO type muxes
# into MP4 — is not an option here. The rate ladder mirrors AudioSaveHelper in
# comfy_api/latest/_ui.py, which solves the same problem for .opus files.
# ---------------------------------------------------------------------------
_OPUS_RATES = (8000, 12000, 16000, 24000, 48000)
_LAYOUTS = {1: "mono", 2: "stereo", 6: "5.1"}


def _opus_sample_rate(sample_rate: int) -> int:
    """Nearest Opus-supported rate at or above `sample_rate`."""
    if sample_rate in _OPUS_RATES:
        return sample_rate
    if sample_rate > 48000:
        return 48000
    return next((rate for rate in _OPUS_RATES if rate > sample_rate), 48000)


def _prepare_audio(audio: dict | None, frame_rate: Fraction, num_frames: int):
    """Resample and trim an AUDIO input for muxing, or return None."""
    if audio is None:
        return None
    waveform = audio.get("waveform")
    if waveform is None or waveform.numel() == 0:
        return None

    waveform = waveform[0]  # [B, C, T] -> [C, T]; only the first batch item
    source_rate = int(audio["sample_rate"])
    sample_rate = _opus_sample_rate(source_rate)

    if sample_rate != source_rate:
        try:
            import torchaudio
        except ImportError as e:
            raise RuntimeError(
                f"Audio is {source_rate} Hz, which Opus — the only audio codec "
                "WebM accepts — does not support. torchaudio is needed to "
                "resample it and is not installed. Either install torchaudio, "
                "or resample the audio to 48000 Hz upstream of this node."
            ) from e
        waveform = torchaudio.functional.resample(waveform, source_rate, sample_rate)

    # Trim so the audio does not outlast the frames it accompanies.
    waveform = waveform[:, : math.ceil((sample_rate / frame_rate) * num_frames)]

    layout = _LAYOUTS.get(waveform.shape[0])
    if layout is None:
        # No layout name for this channel count. Falling back to a name alone is
        # not enough — PyAV checks the array's channel count against the layout —
        # so the data has to be narrowed to match.
        waveform = waveform[:2]
        layout = _LAYOUTS.get(waveform.shape[0], "stereo")

    return waveform, sample_rate, layout


def _add_audio_stream(container, prepared):
    """Declare the Opus stream. Must happen before any packet is muxed.

    Adding a stream after the container has written its header leaves it with a
    zero time_base, and the first mux then fails with "Cannot rebase to zero
    time". Core's VideoFromComponents.save_to declares both streams up front for
    the same reason.
    """
    if prepared is None:
        return None
    _, sample_rate, layout = prepared
    return container.add_stream("libopus", rate=sample_rate, layout=layout)


def _mux_audio(container, stream, prepared) -> None:
    """Encode the whole waveform as one Opus frame and flush."""
    waveform, sample_rate, layout = prepared
    frame = av.AudioFrame.from_ndarray(
        waveform.float().cpu().contiguous().numpy(), format="fltp", layout=layout
    )
    frame.sample_rate = sample_rate
    frame.pts = 0
    container.mux(stream.encode(frame))
    container.mux(stream.encode(None))


# ---------------------------------------------------------------------------
# Temp file recycling
# The temp directory is only wiped when the server starts or stops, never per
# run. A 200-frame 1080p preview is around a megabyte and is re-encoded on every
# queue, so a tuning session would otherwise leave gigabytes behind.
# ---------------------------------------------------------------------------
_LAST_PREVIEW: dict[str, str] = {}


def _recycle(unique_id, new_path: str) -> None:
    """Delete the file this node wrote last time, keeping at most one per node."""
    key = str(unique_id)
    previous = _LAST_PREVIEW.get(key)
    _LAST_PREVIEW[key] = new_path
    if previous and previous != new_path:
        # Safe: the frontend has already moved to the new cache-busted URL, and
        # an in-flight range request holds the old inode open until it finishes.
        with contextlib.suppress(OSError):
            os.remove(previous)


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------
class PreviewWebM(IO.ComfyNode):
    @classmethod
    def define_schema(cls):
        return IO.Schema(
            node_id="PreviewWebM",
            search_aliases=[
                "preview video", "preview webm", "video preview", "play", "scrub",
                "animation preview", "vhs", "video combine",
            ],
            display_name="Preview Video (WebM)",
            description=(
                "Plays an image batch back as a scrubable WebM video inside the "
                "node. The file goes to the temp directory and is replaced on "
                "every run, so nothing is written to your output folder."
            ),
            category="image/video",
            inputs=[
                IO.Image.Input(
                    "images",
                    tooltip="The image batch to play back as a video.",
                ),
                IO.Float.Input(
                    "fps",
                    default=16.0,
                    min=0.01,
                    max=1000.0,
                    step=0.01,
                    tooltip=(
                        "Playback frame rate of the preview.\n"
                        "Defaults to 16, which is what the Wan video models generate at."
                    ),
                ),
                IO.Audio.Input(
                    "audio",
                    optional=True,
                    tooltip=(
                        "Optional audio track, muxed as Opus. Trimmed to the length "
                        "of the image batch. Sample rates Opus does not support are "
                        "resampled to the next supported rate."
                    ),
                ),
                IO.Combo.Input(
                    "quality",
                    options=["fast", "balanced", "high"],
                    default="balanced",
                    advanced=True,
                    tooltip=(
                        "Encoder speed against picture quality:\n"
                        "  'fast' — realtime VP9, crf 40. Lowest latency, biggest file.\n"
                        "  'balanced' — crf 34. Roughly twice as fast as the defaults "
                        "SaveWEBM uses.\n"
                        "  'high' — crf 28, more search effort. Use when you need to "
                        "judge fine detail.\n"
                        "This only affects the throwaway preview, never anything you save."
                    ),
                ),
            ],
            hidden=[IO.Hidden.unique_id],
            is_output_node=True,
        )

    @classmethod
    def execute(cls, images, fps: float, audio: dict | None = None,
                quality: str = "balanced") -> IO.NodeOutput:
        if images is None or images.shape[0] == 0:
            raise ValueError("images is empty — there is nothing to preview.")

        num_frames, height, width = images.shape[0], images.shape[1], images.shape[2]
        save_alpha = images.shape[-1] == 4
        crf, deadline, cpu_used, gop_seconds = _PRESETS[quality]
        # An exact rational rate; a raw float drifts over a long batch.
        frame_rate = Fraction(round(fps * 1000), 1000)

        temp_dir = folder_paths.get_temp_directory()
        os.makedirs(temp_dir, exist_ok=True)
        filename = f"previewwebm_{uuid.uuid4().hex[:12]}.webm"
        filepath = os.path.join(temp_dir, filename)

        prepared_audio = _prepare_audio(audio, frame_rate, num_frames)

        with av.open(filepath, mode="w") as container:
            stream = container.add_stream("libvpx-vp9", rate=frame_rate)
            stream.width = width
            stream.height = height
            stream.pix_fmt = "yuva420p" if save_alpha else "yuv420p"
            # libvpx-vp9 only honours crf as a pure quality target when the
            # bitrate is zeroed; otherwise it runs in constrained-quality mode.
            stream.bit_rate = 0
            stream.options = {
                "crf": crf,
                "deadline": deadline,
                "cpu-used": cpu_used,
                "row-mt": "1",
            }
            # Keyframe spacing bounds how far a scrub-bar drag has to seek back.
            stream.gop_size = max(1, round(fps) * gop_seconds)
            stream.thread_count = 0
            stream.thread_type = "AUTO"

            # Declared before the first packet is muxed — see _add_audio_stream.
            audio_stream = _add_audio_stream(container, prepared_audio)

            channels, pixel_format = (4, "rgba") if save_alpha else (3, "rgb24")
            pbar = ProgressBar(num_frames)
            for image in images:
                # .float() first: the intermediate dtype may be fp16, and the
                # tensor may still be on the GPU.
                array = (
                    torch.clamp(image[..., :channels].float() * 255, min=0, max=255)
                    .to(device=torch.device("cpu"), dtype=torch.uint8)
                    .numpy()
                )
                for packet in stream.encode(
                    av.VideoFrame.from_ndarray(array, format=pixel_format)
                ):
                    container.mux(packet)
                pbar.update(1)
            container.mux(stream.encode())

            if audio_stream is not None:
                _mux_audio(container, audio_stream, prepared_audio)

        _recycle(cls.hidden.unique_id, filepath)

        return IO.NodeOutput(
            ui=UI.PreviewVideo([UI.SavedResult(filename, "", IO.FolderType.temp)])
        )


class PreviewWebMExtension(ComfyExtension):
    @override
    async def get_node_list(self) -> list[type[IO.ComfyNode]]:
        return [PreviewWebM]


async def comfy_entrypoint() -> PreviewWebMExtension:
    return PreviewWebMExtension()
