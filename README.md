# ComfyUI-PreviewWebM

A **scrubable inline video preview** for image batches in
[ComfyUI](https://github.com/comfyanonymous/ComfyUI) — with a real seek bar, and
without saving anything.

ComfyUI has `Preview Image`, but no preview for video. The built-in `SaveWEBM`
and `SaveVideo` nodes write into `output/`, which means every time you queue a
sampler to see how the motion looks, you leave a file behind to clean up later.

This node encodes to the temp directory instead and hands the frontend a preview
descriptor, so the player appears in the node and nothing lands in `output/`. It
also deletes its own previous file each run, so a long tuning session doesn't
quietly fill the disk with discarded takes. Assembling and saving the real thing
stays wherever you already do it.

## Node

**Preview Video (WebM)** (category: `image/video`)

| Input | Default | Notes |
|---|---|---|
| `images` | — | The image batch to play back. |
| `fps` | `24.0` | Playback frame rate, defaulting to the native rate of most current video models. Stored as an exact rational, so `23.976` stays `2997/125`. |
| `audio` | *(optional)* | Muxed as Opus, trimmed to the length of the image batch. |
| `quality` *(advanced)* | `near-lossless` | Encoder speed against picture quality. See below. |
| `mute` | `false` | Leave the audio out without unwiring it. The track is skipped at encode time, so the file has no audio stream at all. |

No outputs — wire `images` in from the same socket that feeds your save node.
It is an output node, so it runs on its own.

## Performance

libvpx-vp9's defaults suit neither end of what a preview is for — too slow for a
glance, too lossy for a verdict. 60 frames of 1920x1080, smooth structured
content:

| preset | time | per frame | size |
|---|---:|---:|---:|
| `SaveWEBM` defaults (crf 32), for comparison | 2.24 s | 37.4 ms | 228 KiB |
| `fast` | 0.87 s | 14.5 ms | 674 KiB |
| `balanced` | 1.12 s | 18.7 ms | 186 KiB |
| `high` | 1.30 s | 21.7 ms | 323 KiB |
| `near-lossless` *(default)* | 2.08 s | 34.7 ms | 1443 KiB |

`fast` is around 2.5x quicker than the built-in node's settings at the cost of a
larger throwaway file. `high` spends its budget on picture quality rather than
compression, which is why it is both slower *and* bigger.

`near-lossless` is the default because the common reason to look at a preview is
to decide whether the model got it right, and at a streaming crf the codec's own
artifacts are easy to mistake for the model's. It costs about 7x the bytes of
`balanced` and none of that matters: the file is deleted on the next run, so the
only real cost is time — and even then it lands slightly *under* what `SaveWEBM`
takes at crf 32, because the thread and row-mt settings more than pay for the
lower crf. Drop to `fast` when you only want to see whether the motion reads.

## Installation

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/dreevelle/ComfyUI-PreviewWebM
```

No extra dependencies; PyAV and torch ship with ComfyUI. `torchaudio` is only
needed if you feed in audio at a sample rate Opus does not support, and ComfyUI
already ships it in practice.

## Notes

- **Nothing is saved.** Files go to ComfyUI's temp directory, which is wiped
  when the server starts and stops. Each preview node keeps exactly one file at
  a time and deletes its previous one on the next run.
- **A preview does not survive a restart.** The player will 404 until you queue
  again — the workflow remembers the result, but the temp file is gone.
- **Scrubbing works** because `/view` serves through a range-capable response.
  Keyframes are placed about once per second (once every two seconds on `high`),
  which bounds how far a drag has to seek back.
- **Colour is converted and tagged explicitly** as BT.709, full range. Left to
  its own devices the encoder converts with swscale's BT.601 defaults and writes
  no matrix tag at all, and browsers assume BT.709 for HD — a mismatch that
  rotates saturated hues by up to 15% (pure green arrives as `(0, 216, 0)`).
  Neutrals are unaffected, so it shows up as a tint on saturated colour rather
  than as a contrast shift.
- **`near-lossless` uses 4:4:4 chroma**, which is VP9 profile 1. Chrome and
  Firefox decode it in software; nothing decodes it in hardware. The other three
  presets stay 4:2:0 profile 0. An RGBA batch always stays 4:2:0, because
  libvpx-vp9 offers `yuva420p` and no 4:4:4 equivalent.
- **10-bit is not available** from libvpx-vp9 unless it was built with
  `--enable-vp9-highbitdepth`, so banding in very smooth gradients is the one
  artifact a lower crf cannot fully remove.
- **Alpha is preserved** when the batch is RGBA: the stream is encoded as
  `yuva420p` and the container is tagged `alpha_mode=1`. Note that the alpha
  rides in a separate channel, so tools inspecting the file will report the main
  plane as `yuv420p` — that is normal, not a loss.
- **Audio must be Opus**, because WebM accepts only Vorbis and Opus, not the AAC
  that ComfyUI's MP4 path uses. Sample rates Opus does not support are resampled
  up to the nearest supported one, matching how core writes `.opus` files.
  Channel counts other than 1, 2 and 6 are narrowed to stereo.
- **`mute` drops the track rather than silencing it**, so a muted preview is a
  video-only file. It short-circuits before the resample, which makes it a way
  out if you have audio at a rate Opus cannot take and no `torchaudio` to
  convert it with.
- **Odd dimensions are fine.** Non-even widths and heights encode without any
  padding or cropping.
- **One video per node.** The frontend displays only the first result, so this
  deliberately produces a single file.

## License

[MIT](LICENSE)
