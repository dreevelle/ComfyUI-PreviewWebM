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

The quality presets exist because libvpx-vp9's defaults suit neither end of what
a preview is for. On 60 frames of 1080p the defaults take 1.9 s where the
realtime path takes 0.3 s — too slow when you only want to see whether the
motion reads. At their default crf they also leave artifacts big enough to be
mistaken for the model's — too lossy when the preview is what you are judging
the model by. The presets span both ends: ``fast`` for a glance,
``near-lossless`` for a verdict.

Colour is converted and tagged explicitly rather than left to ``stream.encode``,
whose implicit reformat picks swscale's defaults — BT.601 — and tags no matrix
at all. Chrome assumes BT.709 for HD, and that mismatch rotates saturated hues
by up to 15%: pure green decodes as ``(0, 216, 0)``. Every preset converts to
BT.709 full range and tags what it did.
"""

import contextlib
import math
import os
import uuid
from fractions import Fraction

import av
import torch
from av.video.reformatter import ColorRange, Colorspace
from typing_extensions import override

import folder_paths
from comfy.utils import ProgressBar
from comfy_api.latest import ComfyExtension, IO, UI

__version__ = "1.0.0"


# ---------------------------------------------------------------------------
# Encoder presets
# Maps the quality combo to (crf, deadline, cpu-used, keyframe interval in
# seconds, chroma format). Lower cpu-used means more search effort and a slower
# encode; the keyframe interval sets how far a scrub-bar drag has to seek back.
#
# Only near-lossless takes yuv444p. That is VP9 profile 1, which Chrome and
# Firefox decode in software but nothing decodes in hardware, so the presets
# meant for everyday use stay on profile 0. At crf 15 the point is to remove
# every excuse for an artifact you might otherwise blame on the model, and 4:2:0
# chroma is the largest one left once crf is that low.
# ---------------------------------------------------------------------------
_PRESETS = {
    "fast":          ("40", "realtime", "8", 1, "yuv420p"),
    "balanced":      ("34", "good",     "5", 1, "yuv420p"),
    "high":          ("28", "good",     "2", 2, "yuv420p"),
    "near-lossless": ("15", "good",     "4", 1, "yuv444p"),
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
                    default=24.0,
                    min=0.01,
                    max=1000.0,
                    step=0.01,
                    tooltip=(
                        "Playback frame rate of the preview.\n"
                        "Defaults to 24, the native rate of most current video models."
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
                    options=["fast", "balanced", "high", "near-lossless"],
                    default="near-lossless",
                    advanced=True,
                    tooltip=(
                        "Encoder speed against picture quality:\n"
                        "  'fast' — realtime VP9, crf 40. Lowest latency, biggest file.\n"
                        "  'balanced' — crf 34. Roughly twice as fast as the defaults "
                        "SaveWEBM uses.\n"
                        "  'high' — crf 28, more search effort. Use when you need to "
                        "judge fine detail.\n"
                        "  'near-lossless' — crf 15 and 4:4:4 chroma. Encoder artifacts "
                        "stay below what you can see, so what you are judging is the "
                        "model and not the codec. Slowest and by far the largest, which "
                        "costs nothing here because the file is discarded next run.\n"
                        "This only affects the throwaway preview, never anything you save."
                    ),
                ),
                # Appended last on purpose. The frontend stores widget values as a
                # positional array, so a widget inserted above an existing one
                # shifts every saved value below it — putting this between 'fps'
                # and 'quality' handed 'mute' the quality string, which is truthy,
                # and silently dropped audio from every pre-existing workflow.
                # New widgets go at the end.
                IO.Boolean.Input(
                    "mute",
                    default=False,
                    label_on="muted",
                    label_off="audio on",
                    tooltip=(
                        "Leave the audio out of the preview without unwiring it.\n"
                        "The track is skipped at encode time rather than silenced, so "
                        "the file has no audio stream at all and the encode skips the "
                        "resample too. Has no effect if nothing is wired to 'audio'."
                    ),
                ),
            ],
            hidden=[IO.Hidden.unique_id],
            is_output_node=True,
        )

    @classmethod
    def execute(cls, images, fps: float, audio: dict | None = None,
                mute: bool = False, quality: str = "near-lossless") -> IO.NodeOutput:
        if images is None or images.shape[0] == 0:
            raise ValueError("images is empty — there is nothing to preview.")

        num_frames, height, width = images.shape[0], images.shape[1], images.shape[2]
        save_alpha = images.shape[-1] == 4
        crf, deadline, cpu_used, gop_seconds, chroma = _PRESETS[quality]
        # An exact rational rate; a raw float drifts over a long batch.
        frame_rate = Fraction(round(fps * 1000), 1000)

        temp_dir = folder_paths.get_temp_directory()
        os.makedirs(temp_dir, exist_ok=True)
        filename = f"previewwebm_{uuid.uuid4().hex[:12]}.webm"
        filepath = os.path.join(temp_dir, filename)

        # Short-circuited before _prepare_audio rather than after, so muting also
        # skips the resample — the one part of the audio path that can raise, and
        # the reason mute doubles as an escape hatch when torchaudio is missing.
        prepared_audio = None if mute else _prepare_audio(audio, frame_rate, num_frames)

        with av.open(filepath, mode="w") as container:
            stream = container.add_stream("libvpx-vp9", rate=frame_rate)
            stream.width = width
            stream.height = height
            # Alpha rides in a separate VP9 channel and libvpx-vp9 offers only
            # yuva420p — there is no yuva444p — so an RGBA batch cannot take the
            # 4:4:4 path even on near-lossless.
            pix_fmt = "yuva420p" if save_alpha else chroma
            stream.pix_fmt = pix_fmt
            # libvpx-vp9 only honours crf as a pure quality target when the
            # bitrate is zeroed; otherwise it runs in constrained-quality mode.
            stream.bit_rate = 0
            # These tags and the reformat in the encode loop below must describe
            # the same transform. The decoder inverts using the tags, so tags
            # that disagree with the conversion are worse than no tags at all.
            stream.colorspace = Colorspace.ITU709      # matrix
            stream.color_primaries = 1                 # BT.709, same as sRGB's
            stream.color_trc = 1                       # BT.709 transfer
            stream.color_range = ColorRange.JPEG       # full, no 16-235 squeeze
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
                # Converted here rather than implicitly inside stream.encode(),
                # which would fall back to swscale's defaults: BT.601, and no
                # matrix tag written. See the colour tags on the stream above.
                frame = av.VideoFrame.from_ndarray(array, format=pixel_format).reformat(
                    format=pix_fmt,
                    dst_colorspace=Colorspace.ITU709,
                    src_color_range=ColorRange.JPEG,  # rgb24/rgba are full range
                    dst_color_range=ColorRange.JPEG,
                )
                for packet in stream.encode(frame):
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
